"""Strict Chromium gate against real HTTP services and an independent worker.

Install Playwright Chromium first, or explicitly select an existing Chromium
with --browser-executable. Missing browsers are failures, never skipped checks.
"""
import argparse
from contextlib import ExitStack, suppress
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from e2e_support import QUESTION, Services, clean_environment, eventually, require, write_report


EXPECTED = ["successful_workflow", "evidence_refusal", "unauthorized_access", "approval_separation",
            "stream_view_ownership", "stale_status_response", "stale_submission_response", "credential_lifetime",
            "actionable_request_errors", "ambiguous_submission_retry", "malformed_acknowledgement_retry",
            "submission_identity_lifetime"]


def browser_eventually(page, check, *, timeout=10, description="browser event"):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        require(time.monotonic() < deadline, "Deadline waiting for " + description)
        page.wait_for_timeout(20)  # Dispatch Playwright events while polling external transport state.


def displayed_result(page):
    text = page.locator("#result").inner_text()
    return json.loads(text) if text.startswith("{") else None


def displayed_events(page):
    return [json.loads(line[6:]) for line in page.locator("#events").inner_text().splitlines()
            if line.startswith("data: ") and '"run_id"' in line]


def submit(page, goal):
    previous = page.locator("#run").input_value()
    page.locator("#goal").fill(goal)
    page.locator("#submit").click()
    from playwright.sync_api import expect
    expect(page.locator("#submit")).to_be_enabled()
    run_id = page.locator("#run").input_value()
    require(bool(run_id) and run_id != previous, "UI submission did not load a new workflow")
    require(not page.locator("#notice").inner_text(), "UI submission displayed an error")
    return run_id


def refresh(page, run_id, status):
    if page.locator("#run").input_value() != run_id:
        page.locator("#run").fill(run_id)
    page.locator("#refresh").click()
    from playwright.sync_api import expect
    expect(page.locator("#refresh")).to_be_enabled()
    result = displayed_result(page)
    require(result and result.get("id") == run_id and result.get("status") == status,
            "UI refresh disagrees with the expected durable state")
    return result


def follow(page, services, run_id, *, cursor=None, token=None):
    path = services.agent_url + f"/workflows/{run_id}/events/stream"
    with page.expect_request(lambda request: request.url == path) as sent:
        page.locator("#stream").click()
    headers = sent.value.all_headers()
    require(headers.get("last-event-id") == cursor, "Event cursor leaked across workflow or credential ownership")
    require(headers.get("authorization") == "Bearer " + (token or services.key), "Stream used a stale credential")


def await_terminal_stream(page, run_id):
    from playwright.sync_api import expect
    expect(page.locator("#events")).to_contain_text("event: done")
    expect(page.locator("#stream")).to_be_enabled()
    events = displayed_events(page)
    require(bool(events) and all(event["run_id"] == run_id for event in events), "UI mixed workflows in an event stream")
    require(len({event["id"] for event in events}) == len(events), "UI duplicated a durable event")
    return events


def flush_render(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def screenshot(page, artifacts, report, name):
    path = artifacts / name
    page.screenshot(path=str(path), full_page=True)
    report["screenshots"].append(str(path))


def click_submission(page, services):
    """Submit the unchanged form, preserving the meaning of a user's retry."""
    from playwright.sync_api import expect
    with page.expect_request(lambda request: request.method == "POST" and
                             request.url == services.agent_url + "/workflows") as sent:
        page.locator("#submit").click()
    expect(page.locator("#submit")).to_be_enabled()
    payload = sent.value.post_data_json
    require(re.fullmatch(r"ui\.[0-9a-f]{32}", payload.get("idempotency_key", "")), "Missing cryptographic submission identity")
    require(payload["idempotency_key"] not in sent.value.url, "Submission identity leaked into the URL")
    return payload, displayed_result(page)


def assert_single_execution(services, goal, run_id, *, token=None):
    state = services.finish(run_id, token=token)
    require(state["status"] == "completed", "Retried real workflow did not complete")
    runs = services.request("GET", "/workflows", token=token)
    require([run["id"] for run in runs if run["goal"] == goal] == [run_id], "Retry created another durable workflow")
    require(len(state["steps"]) == 1 and state["steps"][0]["attempts"] == 1, "Retry repeated durable execution")
    events = services.request("GET", f"/workflows/{run_id}/events", token=token)
    require(sum(event["type"] == "created" for event in events) == 1 and
            sum(event["type"] == "started" for event in events) == 1, "Retry added another execution event")


def run_submission_polish(page, services, report, artifacts):
    from playwright.sync_api import expect

    url = services.agent_url + "/workflows"
    page.locator("#token").fill(services.key)
    page.locator("#goal").fill("x")
    before = {run["id"] for run in services.request("GET", "/workflows")}
    first, result = click_submission(page, services)
    require(result is None, "Rejected submission displayed an accepted result")
    notice = page.locator("#notice").inner_text()
    require("goal" in notice and "at least 3 characters" in notice and "[object Object]" not in notice,
            "Actual field-validation response was not actionable")
    require(len(notice) <= 800, "Validation notice is unbounded")
    second, _ = click_submission(page, services)
    require(first["idempotency_key"] != second["idempotency_key"], "Definitive first rejection kept an unresolved identity")
    require({run["id"] for run in services.request("GET", "/workflows")} == before, "Validation rejection created a workflow")
    screenshot(page, artifacts, report, "validation-error.png")

    page.route(url, lambda route: route.fulfill(status=503, content_type="text/html",
               body="<h1>fixture intermediary response</h1>"), times=1)
    failed, _ = click_submission(page, services)
    notice = page.locator("#notice").inner_text()
    require("HTTP 503" in notice and "unconfirmed" in notice and "fixture intermediary" not in notice,
            "Non-JSON transport error did not use the safe HTTP fallback")
    rejected, _ = click_submission(page, services)  # Actual 422 cannot resolve the earlier 503 ambiguity.
    repeated, _ = click_submission(page, services)
    require(failed["idempotency_key"] == rejected["idempotency_key"] == repeated["idempotency_key"],
            "A later rejection discarded an earlier uncertain submission identity")
    report["checks"].append({"id": EXPECTED[8], "passed": True, "real_validation_status": 422,
                             "non_json_status": 503, "messages_bounded": True, "earlier_uncertainty_preserved": True})

    committed = []

    def discard_committed_response(route):
        actual = route.fetch()
        require(actual.status == 202, "Transport fault did not follow an actual committed submission")
        committed.append(actual.json())
        route.abort("connectionreset")

    goal = "Calculate 31 + 11"
    page.locator("#goal").fill(goal)
    page.route(url, discard_committed_response, times=1)
    original, result = click_submission(page, services)
    require(result is None and len(committed) == 1, "Lost response unexpectedly acknowledged the submission")
    expect(page.locator("#notice")).to_contain_text("unconfirmed")
    run_id = committed[-1]["id"]
    assert_single_execution(services, goal, run_id)
    # Re-entering the exact same value does not create a different logical request.
    page.locator("#goal").fill(goal)
    retried, acknowledged = click_submission(page, services)
    require(retried == original and acknowledged == {"id": run_id, "duplicate": True},
            "Lost committed response was not recovered using the original submission")
    assert_single_execution(services, goal, run_id)
    screenshot(page, artifacts, report, "retried-submission.png")
    report["checks"].append({"id": EXPECTED[9], "passed": True, "real_committed_response_discarded": True,
                             "duplicate_recognized": True, "durable_runs_after_retry": 1, "step_attempts": 1})

    fresh, acknowledged = click_submission(page, services)
    require(fresh["idempotency_key"] != original["idempotency_key"] and acknowledged["id"] != run_id
            and acknowledged["duplicate"] is False, "Acknowledged success prevented an intentional new workflow")
    require(services.finish(acknowledged["id"])["status"] == "completed", "Intentional new submission failed")

    malformed_cases = ("invalid_json", "invalid_id", "invalid_unicode_id", "path_id", "control_id",
                       "invalid_duplicate", "unexpected_status")
    for index, kind in enumerate(malformed_cases):
        goal = f"Calculate 40 + {index + 1}"
        page.locator("#goal").fill(goal)
        committed.clear()

        def corrupt_acknowledgement(route):
            actual = route.fetch()
            require(actual.status == 202, "Malformed acknowledgement did not follow a real submission")
            value = actual.json()
            committed.append(value)
            bad_ids = {"invalid_id": 123, "invalid_unicode_id": "\ud800", "path_id": "../" + value["id"],
                       "control_id": value["id"] + "\x00"}
            broken = {**value, "id": bad_ids[kind]} if kind in bad_ids else {**value, "duplicate": "false"}
            body = "{" if kind == "invalid_json" else json.dumps(value if kind == "unexpected_status" else broken)
            route.fulfill(status=200 if kind == "unexpected_status" else 202, content_type="application/json", body=body)

        page.route(url, corrupt_acknowledgement, times=1)
        original, result = click_submission(page, services)
        require(result is None and len(committed) == 1, "Malformed success was treated as acknowledged")
        expect(page.locator("#notice")).to_contain_text("unconfirmed")
        retried, acknowledged = click_submission(page, services)
        require(retried == original and acknowledged == {"id": committed[0]["id"], "duplicate": True},
                "Malformed successful response caused a fresh submission identity")
        assert_single_execution(services, goal, committed[0]["id"])
    report["checks"].append({"id": EXPECTED[10], "passed": True, "variants": list(malformed_cases),
                             "actual_commits_recovered": len(malformed_cases), "repeated_executions": 0})

    # A changed logical request gets a fresh identity; these faults occur before forwarding.
    for index, change in enumerate(("goal", "mode", "credential")):
        page.locator("#token").fill(services.key)
        page.locator("#mode").select_option("dag")
        page.locator("#goal").fill(f"Calculate 60 + {index + 1}")
        page.route(url, lambda route: route.abort("connectionreset"), times=1)
        original, _ = click_submission(page, services)
        token = services.key
        if change == "goal":
            page.locator("#goal").fill("Calculate 60 + 9")
        elif change == "mode":
            page.locator("#mode").select_option("agent_loop")
        else:
            token = services.other_key
            page.locator("#token").fill(token)
        changed, acknowledged = click_submission(page, services)
        require(changed["idempotency_key"] != original["idempotency_key"] and acknowledged["duplicate"] is False,
                "Changed " + change + " reused an unresolved submission")
        require(services.finish(acknowledged["id"], token=token)["status"] == "completed", "Changed request failed")

    # A stale acknowledged response must not clear the newer pending identity.
    page.locator("#token").fill(services.key)
    page.locator("#mode").select_option("dag")
    older_goal, later_goal = "Calculate 80 + 1", "Calculate 80 + 2"
    page.locator("#goal").fill(older_goal)
    delayed = []
    committed.clear()

    def overlapping_acknowledgements(route):
        require(route.request.method == "POST", "Unexpected request during overlapping submissions")
        goal = route.request.post_data_json["goal"]
        if goal == older_goal:
            require(not delayed, "Older submission was intercepted twice")
            actual = route.fetch()
            require(actual.status == 202, "Held response was not a real acknowledgement")
            delayed.append((route, actual))
        else:
            require(goal == later_goal and not committed, "Unexpected newer submission")
            discard_committed_response(route)

    # Expiring the last handler resumes other in-flight routes. Keep this handler
    # registered until both the delayed and deliberately lost responses settle.
    page.route(url, overlapping_acknowledgements)
    page.locator("#submit").click()
    browser_eventually(page, lambda: delayed, description="older actual acknowledgement held")
    page.locator("#goal").fill(later_goal)
    newer, result = click_submission(page, services)
    require(result is None and len(committed) == 1, "Newer committed response was not discarded")
    with page.expect_response(lambda response: response.url == url and
                              response.request.post_data_json["goal"] == older_goal) as delivered:
        delayed[0][0].fulfill(response=delayed[0][1])
    delivered.value.finished()
    page.unroute(url, overlapping_acknowledgements)
    flush_render(page)
    require(displayed_result(page) is None and page.locator("#run").input_value() == "",
            "Obsolete acknowledgement overwrote the unresolved newer submission")
    retry, acknowledged = click_submission(page, services)
    require(retry == newer and acknowledged == {"id": committed[0]["id"], "duplicate": True},
            "Obsolete acknowledgement cleared the newer pending identity")
    assert_single_execution(services, later_goal, committed[0]["id"])
    require(services.finish(delayed[0][1].json()["id"])["status"] == "completed", "Older actual submission did not execute")

    page.locator("#goal").fill("Calculate 90 + 1")
    page.route(url, lambda route: route.abort("connectionreset"), times=1)
    in_memory, _ = click_submission(page, services)
    storage = page.evaluate("() => ({local: {...localStorage}, session: {...sessionStorage}})")
    require(storage == {"local": {}, "session": {}} and not page.context.cookies(), "Pending identity persisted in browser storage")
    page.reload()
    expect(page.locator("#token")).to_have_value("")
    page.locator("#token").fill(services.key)
    page.locator("#goal").fill("Calculate 90 + 1")
    reloaded, acknowledged = click_submission(page, services)
    require(reloaded["idempotency_key"] != in_memory["idempotency_key"], "Submission identity survived a page reload")
    require(services.finish(acknowledged["id"])["status"] == "completed", "Reloaded submission failed")
    report["checks"].append({"id": EXPECTED[11], "passed": True, "fresh_after_acknowledgement": True,
                             "changed_inputs": ["goal", "mode", "credential"], "obsolete_acknowledgement_fenced": True,
                             "storage_empty_while_pending": True, "fresh_after_reload": True})


def run(page, services, report, artifacts):
    from playwright.sync_api import expect

    credentials = [services.key, services.other_key, services.approver]
    observed = {"url_leak": False, "body_leak": False, "workflow_requests": 0, "missing_auth": False}

    def inspect_request(request):
        observed["url_leak"] |= any(key in request.url for key in credentials)
        observed["body_leak"] |= any(key in (request.post_data or "") for key in credentials)
        if urlsplit(request.url).path.startswith("/workflows"):
            observed["workflow_requests"] += 1
            observed["missing_auth"] |= not request.headers.get("authorization", "").startswith("Bearer ")

    page.on("request", inspect_request)
    response = page.goto(services.agent_url)
    policy = response.headers.get("content-security-policy", "")
    require("script-src 'sha256-" in policy and "connect-src 'self'" in policy, "Dashboard CSP was weakened")
    require("'unsafe-eval'" not in policy and "script-src 'unsafe-inline'" not in policy, "Unsafe script CSP")
    page.locator("#token").fill(services.key)
    positive = submit(page, QUESTION)
    require(services.finish(positive)["status"] == "completed", "Actual browser workflow failed")
    state = refresh(page, positive, "completed")
    answer = state["steps"][-1]["result"]
    require("30 minutes" in answer["answer"] and bool(answer["citations"]), "UI answer lacks real retrieval evidence")
    follow(page, services, positive)
    await_terminal_stream(page, positive)
    screenshot(page, artifacts, report, "successful-workflow.png")
    report["checks"].append({"id": EXPECTED[0], "passed": True, "actual_evidence": True, "terminal_stream": True})

    page.locator("#token").fill(services.other_key)
    refusal = submit(page, QUESTION)
    require(services.finish(refusal, token=services.other_key)["status"] == "failed", "Empty tenant did not refuse")
    state = refresh(page, refusal, "failed")
    result = state["steps"][-1]["result"]
    require(result["outcome"] == "insufficient_evidence" and not result["citations"]
            and result["answer"] == "Insufficient evidence to answer the request.",
            "UI lost the typed refusal or fabricated an answer")
    follow(page, services, refusal, token=services.other_key)
    await_terminal_stream(page, refusal)
    screenshot(page, artifacts, report, "evidence-refusal.png")
    report["checks"].append({"id": EXPECTED[1], "passed": True, "factual_answer": False, "terminal_status": "failed"})

    page.locator("#token").fill("invalid-browser-fixture")
    page.locator("#run").fill(positive)
    page.locator("#refresh").click()
    expect(page.locator("#notice")).to_have_text("Invalid credential")
    require(displayed_result(page) is None, "Unauthorized request retained private result data")
    page.locator("#token").fill(services.other_key)
    page.locator("#refresh").click()
    expect(page.locator("#notice")).to_have_text("Workflow not found")
    require(displayed_result(page) is None, "Other tenant read a private workflow")
    report["checks"].append({"id": EXPECTED[2], "passed": True, "invalid_credential": 401, "other_tenant": 404})

    publication = services.submit("Publish verified browser arithmetic", steps=[
        {"id": "calc", "tool": "math.calculate", "args": {"expression": "6 * 7"}},
        {"id": "publish", "tool": "artifact.publish", "depends_on": ["calc"]},
    ])
    services.finish(publication, statuses=("waiting_approval",))
    page.locator("#token").fill(services.key)
    refresh(page, publication, "waiting_approval")
    page.locator("#step").fill("publish")
    page.locator("#approve").click()
    expect(page.locator("#notice")).to_have_text("Permission denied")
    require(services.state(publication)["status"] == "waiting_approval", "Requester bypassed approval separation")
    page.locator("#token").fill(services.approver)
    page.locator("#approve").click()
    expect(page.locator("#approve")).to_be_enabled()
    require(displayed_result(page).get("approved") is True, "Approver UI did not authorize publication")
    state = services.finish(publication)
    require(state["status"] == "completed", "Approved publication failed")
    artifact = state["steps"][-1]["result"]["artifact_ref"]["artifact_id"]
    require(services.request("GET", "/artifacts/" + artifact)["calc"]["value"] == 42, "Published artifact is incorrect")
    refresh(page, publication, "completed")
    screenshot(page, artifacts, report, "approved-publication.png")
    report["checks"].append({"id": EXPECTED[3], "passed": True, "requester_rejected": True, "artifact_value": 42})

    page.locator("#token").fill(services.key)
    first_goal = "What is the session timeout for the first browser stream?"
    second_goal = "What is the session timeout for the second browser stream?"
    first_fault = services.proxy.fault(first_goal, hold_requests=1)
    second_fault = services.proxy.fault(second_goal, hold_requests=1)
    first = submit(page, first_goal)
    eventually(lambda: first_fault.forwarded == 1, description="real first browser RAG request")
    follow(page, services, first)
    expect(page.locator("#events")).to_contain_text('"type": "started"')
    page.locator("#stop").click()
    expect(page.locator("#stream")).to_be_enabled()
    cursor = re.findall(r"^id: (\d+)$", page.locator("#events").inner_text(), re.MULTILINE)[-1]
    follow(page, services, first, cursor=cursor)
    expect(page.locator("#stream")).to_be_disabled()

    second = submit(page, second_goal)
    # Old UI left the first stream open and this button disabled indefinitely.
    expect(page.locator("#stream")).to_be_enabled(timeout=3000)
    expect(page.locator("#events")).to_have_text("")
    follow(page, services, second)
    expect(page.locator("#events")).to_contain_text(second)
    first_fault.release.set()
    require(services.finish(first)["status"] == "completed", "Released prior workflow did not finish")
    eventually(lambda: second_fault.forwarded == 1, description="real second browser RAG request")
    expect(page.locator("#events")).to_contain_text('"type": "started"')
    require(all(event["run_id"] == second for event in displayed_events(page)), "Late prior events polluted the new view")
    page.locator("#run").fill(first)
    expect(page.locator("#stream")).to_be_enabled()
    follow(page, services, first)
    await_terminal_stream(page, first)
    page.locator("#run").fill(second)
    follow(page, services, second)
    expect(page.locator("#events")).to_contain_text('"type": "started"')
    page.locator("#token").fill(services.approver)
    expect(page.locator("#stream")).to_be_enabled()
    expect(page.locator("#events")).to_have_text("")
    follow(page, services, second, token=services.approver)
    second_fault.release.set()
    require(services.finish(second)["status"] == "completed", "Second browser workflow failed")
    events = await_terminal_stream(page, second)
    require(events == services.events(second), "Credential-owned stream differs from durable backend events")
    screenshot(page, artifacts, report, "stream-isolation.png")
    report["checks"].append({"id": EXPECTED[4], "passed": True, "same_run_cursor_reused": True,
                             "cross_run_cursor_reused": False, "credential_change_aborts": True,
                             "old_events_in_new_view": 0})

    # Buffer genuine API responses at the browser transport, without mocking
    # service behavior, so view changes occur before an older fetch resolves.
    page.locator("#token").fill(services.key)
    page.locator("#run").fill(first)
    delayed = []
    old_status_url = services.agent_url + "/workflows/" + first

    def hold_response(route):
        delayed.append((route, route.fetch()))

    page.route(old_status_url, hold_response, times=1)
    page.locator("#refresh").click()
    browser_eventually(page, lambda: delayed, description="real status response buffered")
    page.locator("#run").fill(second)
    refresh(page, second, "completed")
    with page.expect_response(lambda response: response.url == old_status_url) as delivered:
        delayed[0][0].fulfill(response=delayed[0][1])
    delivered.value.finished()
    flush_render(page)
    require(displayed_result(page)["id"] == second, "Stale status fetch overwrote the new workflow")
    report["checks"].append({"id": EXPECTED[5], "passed": True, "actual_response_delayed": True})

    delayed.clear()
    submission_url = services.agent_url + "/workflows"
    page.route(submission_url, hold_response, times=1)
    page.locator("#goal").fill("Calculate 9 + 4")
    page.locator("#submit").click()
    browser_eventually(page, lambda: delayed, description="real submission response buffered")
    created = delayed[0][1].json()["id"]
    page.locator("#token").fill(services.approver)
    page.locator("#run").fill(second)
    refresh(page, second, "completed")
    with page.expect_response(lambda response: response.url == submission_url) as delivered:
        delayed[0][0].fulfill(response=delayed[0][1])
    delivered.value.finished()
    flush_render(page)
    require(page.locator("#run").input_value() == second and displayed_result(page)["id"] == second,
            "Stale submission replaced the view after credentials changed")
    require(services.finish(created)["status"] == "completed", "Buffered response did not originate from a real submission")
    report["checks"].append({"id": EXPECTED[6], "passed": True, "actual_submission_completed": True})

    require(observed["workflow_requests"] > 20 and not observed["missing_auth"], "Browser authentication was not exercised")
    require(not observed["url_leak"] and not observed["body_leak"], "Credential leaked outside Authorization headers")
    storage = page.evaluate("() => ({local: {...localStorage}, session: {...sessionStorage}})")
    require(storage == {"local": {}, "session": {}}, "Credentials were persisted in browser storage")
    require(not page.context.cookies(), "Browser credentials unexpectedly persisted in cookies")
    page.reload()
    expect(page.locator("#token")).to_have_value("")
    require(displayed_result(page) is None, "Private workflow content survived reload")
    report["checks"].append({"id": EXPECTED[7], "passed": True, "authorization_header_only": True,
                             "storage_empty": True, "credential_absent_after_reload": True})
    run_submission_polish(page, services, report, artifacts)
    require(not observed["url_leak"] and not observed["body_leak"] and not observed["missing_auth"],
            "Submission retry leaked a credential or dropped authentication")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--artifacts", required=True, help="Directory for screenshots and browser diagnostics")
    parser.add_argument("--browser-executable", help="Explicit existing Chromium executable; default is Playwright Chromium")
    args = parser.parse_args()
    started = time.perf_counter()
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    services = Services(step_timeout=45)
    browser = None
    report = {"schema_version": "agent-browser-e2e-v1", "status": "failed", "expected_checks": EXPECTED,
              "checks": [], "backend": "sqlite", "real_services": ["rag-http", "agent-http", "independent-worker", "chromium"],
              "live_paid_provider": False, "page_errors": [], "screenshots": []}
    try:
        from playwright.sync_api import sync_playwright
        with services, sync_playwright() as playwright, ExitStack() as cleanup:
            browser = playwright.chromium.launch(headless=True, executable_path=args.browser_executable,
                                                  env=clean_environment())
            cleanup.callback(browser.close)
            report["browser_version"] = browser.version
            context = browser.new_context(viewport={"width": 1280, "height": 1050})
            cleanup.callback(context.close)
            page = context.new_page()
            page.set_default_timeout(10_000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)[:1000]))
            try:
                run(page, services, report, artifacts)
                require(not report["page_errors"], "Browser raised an uncaught JavaScript error")
            except BaseException:
                with suppress(Exception):
                    screenshot(page, artifacts, report, "failure.png")
                    report["browser_failure"] = {"notice": page.locator("#notice").inner_text()[:1000],
                                                 "result": page.locator("#result").inner_text()[:2000]}
                raise
        require([check["id"] for check in report["checks"]] == EXPECTED, "An expected browser check did not execute")
        report["status"] = "passed"
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:750],
                           "diagnostic_tail": str(error)[-1500:]}
        report["service_diagnostics"] = services.diagnostics
        raise
    finally:
        report["browser_closed"] = browser is None or not browser.is_connected()
        report["cleanup_completed"] = services.cleaned and report["browser_closed"]
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_report(args.report, report)
        print(json.dumps({"status": report["status"], "completed_checks": len(report["checks"]),
                          "expected_checks": len(EXPECTED), "report": args.report}, indent=2))


if __name__ == "__main__":
    main()
