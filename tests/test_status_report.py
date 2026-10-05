"""Status report: the dashboard's tabs and spend as a Slack message, sent on request and by its Refresh button. Slack is faked."""

import os
import unittest
from unittest.mock import patch

import tests.test_brief_versions as brief_tests
from backend.services import status_report
from backend.services.catalog import TABS
from backend.services.review import handle_block_action
from backend.services.status_report import MAX_LISTED, build_report, send_report
from tests.test_catalog_imports import product
from tests.test_review import ELLIE

SKU = "VASE-042"
STATUS, REVIEW = "CSTATUS", "C1"


def catalog_of(rows, images=0):
    """A get_catalog()-shaped response for rows given as (sku, name, stage)."""
    rows = [{"sku": sku, "product_name": name, "stage": stage} for sku, name, stage in rows]
    return {
        "rows": rows, "total_rows": len(rows), "spend": {"images": images, "est_cost_usd": round(images * 0.0644, 4)},
        "tabs": [{"id": tab, "label": label, "stages": list(stages),
                  "count": sum(row["stage"] in stages for row in rows)} for tab, label, stages in TABS],
    }


def sections(blocks):
    """The tab sections' text, in order (the first section is the spend line)."""
    return [block["text"]["text"] for block in blocks if block["type"] == "section"][1:]


def refresh_click(user=ELLIE, channel=STATUS):
    return {"user": {"id": user}, "channel": {"id": channel}, "actions": [{"action_id": "refresh_status"}]}


class BuildReportTests(unittest.TestCase):
    def test_tabs_appear_in_dashboard_order_with_their_counts_and_products(self):
        catalog = catalog_of([
            ("B-2", "Bowl", "with_ellie"), ("A-1", "Vase", "with_ellie"), ("C-3", "Lamp", "ready"),
            ("D-4", "Mug", "failed"), ("E-5", "Tray", "in_drive"), ("F-6", "", "needs_input")], images=64)
        text, blocks = build_report(catalog)
        tabs = sections(blocks)
        self.assertEqual([tab.split("\n")[0] for tab in tabs],
                         ["*To generate* (2)", "*Generating* (0)", "*Not posted* (0)", "*With Ellie* (2)",
                          "*To Drive* (0)", "*In Drive* (1)", "*Missing attributes* (1)"])
        self.assertEqual([tab.split("\n")[0] for tab in tabs], [f"*{tab['label']}* ({tab['count']})" for tab in catalog["tabs"]])
        self.assertEqual(tabs[0].split("\n")[1:], ["• C-3 · Lamp", "• D-4 · Mug"])
        self.assertEqual(tabs[3].split("\n")[1:], ["• A-1 · Vase", "• B-2 · Bowl"])  # Ordered by SKU.
        self.assertEqual(tabs[1].split("\n")[1:], ["_None_"])
        self.assertEqual(tabs[6].split("\n")[1:], ["• F-6"])
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("6 products", blocks[0]["text"]["text"])
        self.assertIn("6 products", text)

    def test_spend_line_matches_the_catalog(self):
        _, blocks = build_report(catalog_of([("A-1", "Vase", "ready")], images=64))
        self.assertEqual(blocks[1]["text"]["text"], "Generation spend (estimate): 64 images · about $4.12")
        _, blocks = build_report(catalog_of([("A-1", "Vase", "ready")], images=1))
        self.assertEqual(blocks[1]["text"]["text"], "Generation spend (estimate): 1 image · about $0.06")

    def test_last_block_is_the_refresh_button_without_a_confirm(self):
        _, blocks = build_report(catalog_of([("A-1", "Vase", "ready")]))
        self.assertEqual(blocks[-1]["type"], "actions")
        (button,) = blocks[-1]["elements"]
        self.assertEqual((button["action_id"], button["text"]["text"]), ("refresh_status", "Refresh"))
        self.assertNotIn("confirm", button)
        self.assertNotIn("Requested by", str(blocks))

    def test_requester_is_named(self):
        _, blocks = build_report(catalog_of([("A-1", "Vase", "ready")]), requested_by="UMAYA")
        self.assertEqual(blocks[-2]["elements"][0]["text"], "Requested by <@UMAYA>")

    def assert_within_limits(self, blocks):
        self.assertLessEqual(len(blocks), 50)
        for block in blocks:
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)

    def test_a_large_tab_is_capped_and_counts_the_rest(self):
        rows = [(f"SKU-{n:04d}", f"Product number {n}", "needs_input") for n in range(300)]
        _, blocks = build_report(catalog_of(rows + [("A-1", "Vase", "ready")]))
        self.assert_within_limits(blocks)
        lines = sections(blocks)[6].split("\n")
        self.assertEqual(lines[0], "*Missing attributes* (300)")
        self.assertEqual(lines[1:-1], [f"• SKU-{n:04d} · Product number {n}" for n in range(MAX_LISTED)])
        self.assertEqual(lines[-1], f"_and {300 - MAX_LISTED} more_")

    def test_a_tab_at_the_cap_lists_everything(self):
        _, blocks = build_report(catalog_of([(f"S-{n:02d}", "Vase", "ready") for n in range(MAX_LISTED)]))
        self.assertEqual(len(sections(blocks)[0].split("\n")), MAX_LISTED + 1)
        self.assertNotIn("more_", sections(blocks)[0])

    def test_very_long_names_stay_within_the_section_limit(self):
        # Every character escapes to five, and every line carries a long link.
        rows = [(f"SKU-{n:04d}", "&" * 5000, "with_ellie") for n in range(300)]
        links = {sku: "https://slack.test/archives/C1/p" + "1" * 200 for sku, _, _ in rows}
        _, blocks = build_report(catalog_of(rows), links)
        self.assert_within_limits(blocks)
        lines = sections(blocks)[3].split("\n")
        listed = len(lines) - 2
        self.assertTrue(0 < listed < MAX_LISTED)  # Fewer than the cap fit; the rest are still counted.
        self.assertEqual(lines[-1], f"_and {300 - listed} more_")
        self.assertTrue(lines[1].endswith("…"))

    def test_names_are_escaped_for_slack(self):
        _, blocks = build_report(catalog_of([("A-1", "Salt & Pepper <set> <!channel>", "ready")]))
        self.assertEqual(sections(blocks)[0].split("\n")[1], "• A-1 · Salt &amp; Pepper &lt;set&gt; &lt;!channel&gt;")

    def test_with_ellie_lines_link_to_the_thread_when_a_link_is_given(self):
        catalog = catalog_of([("A-1", "Vase", "with_ellie"), ("B-2", "Bowl", "with_ellie"), ("C-3", "Lamp", "ready")])
        _, blocks = build_report(catalog, {"A-1": "https://slack.test/t1", "C-3": "https://slack.test/t3"})
        tabs = sections(blocks)
        self.assertEqual(tabs[3].split("\n")[1:], ["• <https://slack.test/t1|A-1> · Vase", "• B-2 · Bowl"])
        self.assertEqual(tabs[0].split("\n")[1:], ["• C-3 · Lamp"])  # Only With Ellie links.


class StatusReportTests(unittest.TestCase):
    # The catalog (VASE-042 and LAMP-7), the fakes and the helpers are the brief version tests'.
    preview, confirm, change, row, until, generate, approve, deliver = (
        getattr(brief_tests.BriefVersionTests, name)
        for name in ("preview", "confirm", "change", "row", "until", "generate", "approve", "deliver"))

    def setUp(self):
        brief_tests.BriefVersionTests.setUp(self)
        self.env(SLACK_STATUS_CHANNEL_ID=STATUS, SLACK_APPROVER_USER_ID="")

    def env(self, **values):
        env = patch.dict(os.environ, values)
        env.start()
        self.addCleanup(env.stop)

    def reports(self):
        return [post for post in self.slack.posts if post["channel"] == STATUS]

    def test_send_report_posts_one_message_to_the_status_channel_only(self):
        result = send_report()
        self.assertEqual([post["channel"] for post in self.slack.posts], [STATUS])
        report = self.slack.posts[0]
        self.assertIsNone(report["thread_ts"])
        self.assertEqual(sections(report["blocks"])[0], "*To generate* (2)\n• LAMP-7 · Ceramic vase\n• VASE-042 · Ceramic vase")
        self.assertEqual((result["sent"], result["products"]), (True, 2))

    def test_report_never_goes_to_the_review_channel(self):
        self.generate()
        review_posts = len(self.slack.posts)
        send_report()
        self.assertEqual({post["channel"] for post in self.slack.posts[:review_posts]}, {REVIEW})
        self.assertEqual([post["channel"] for post in self.slack.posts[review_posts:]], [STATUS])

    def test_with_ellie_products_link_to_their_thread(self):
        self.generate()
        send_report()
        self.assertEqual(self.slack.permalinks, [(REVIEW, "100.1")])  # Only the listed With Ellie product.
        self.assertEqual(sections(self.reports()[0]["blocks"])[3],
                         "*With Ellie* (1)\n• <https://slack.test/archives/C1/p1001|VASE-042> · Ceramic vase")

    def test_permalink_failure_still_sends_the_report_without_the_link(self):
        self.generate()
        self.slack.fail_permalinks = True
        send_report()
        self.assertEqual(sections(self.reports()[0]["blocks"])[3], "*With Ellie* (1)\n• VASE-042 · Ceramic vase")

    def test_route_posts_once(self):
        response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["products"], 2)
        self.assertEqual(len(self.reports()), 1)
        self.assertEqual(len(self.slack.posts), 1)

    def test_route_reports_a_missing_status_channel(self):
        self.env(SLACK_STATUS_CHANNEL_ID="")
        response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 503)
        self.assertIn("SLACK_STATUS_CHANNEL_ID is not configured", response.json()["detail"])
        self.assertEqual(self.slack.posts, [])

    def test_route_reports_a_missing_bot_token(self):
        self.env(SLACK_BOT_TOKEN="")
        response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 503)
        self.assertIn("SLACK_BOT_TOKEN is not configured", response.json()["detail"])

    def test_route_reports_an_empty_catalog(self):
        with patch.object(status_report, "get_catalog", return_value=catalog_of([])):
            response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Import a catalog first", response.json()["detail"])
        self.assertEqual(self.slack.posts, [])

    def test_route_surfaces_a_slack_failure(self):
        self.slack.fail_posts = 1
        response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Slack did not accept the status report: slack is down.")
        self.assertEqual(self.slack.posts, [])

    def test_bot_not_in_the_channel_says_to_invite_it(self):
        class NotInChannel(Exception):
            response = {"ok": False, "error": "not_in_channel"}

        with patch.object(self.slack, "post_message", side_effect=NotInChannel()):
            response = self.client.post("/api/status-report")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Invite it to that channel", response.json()["detail"])

    def test_refresh_posts_a_new_report_naming_the_requester(self):
        self.client.post("/api/status-report")
        self.assertEqual(handle_block_action(refresh_click("UMAYA"), self.slack), "sent")
        first, second = self.reports()
        self.assertNotIn("Requested by", str(first["blocks"]))
        self.assertEqual(second["blocks"][-2]["elements"][0]["text"], "Requested by <@UMAYA>")
        self.assertEqual(second["blocks"][-1]["elements"][0]["action_id"], "refresh_status")
        self.assertEqual((self.slack.updates, self.slack.ephemerals), ([], []))  # The old report is left as it was.

    def test_only_the_approver_can_refresh_when_one_is_set(self):
        self.env(SLACK_APPROVER_USER_ID=ELLIE)
        self.assertEqual(handle_block_action(refresh_click("UINTERN"), self.slack), "unauthorized")
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.slack.ephemerals, [("UINTERN", "Only the designated approver can refresh the status report.")])
        self.assertEqual(handle_block_action(refresh_click(ELLIE), self.slack), "sent")
        self.assertEqual(len(self.reports()), 1)

    def test_anyone_can_refresh_when_no_approver_is_set(self):
        for user in ("UMAYA", "UINTERN"):
            self.assertEqual(handle_block_action(refresh_click(user), self.slack), "sent")
        self.assertEqual(len(self.reports()), 2)

    def test_failed_refresh_tells_the_clicker_privately(self):
        self.slack.fail_posts = 1
        self.assertEqual(handle_block_action(refresh_click("UMAYA"), self.slack), "failed")
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.slack.ephemerals,
                         [("UMAYA", "The status report was not sent. Slack did not accept the status report: slack is down.")])

    def test_report_and_catalog_agree_after_a_generation_and_an_approval(self):
        self.approve()
        self.generate("LAMP-7")
        self.confirm(self.preview([product("BLANK", shot_idea=""), product("MUG-1")]))
        send_report()
        catalog = self.client.get("/api/catalog").json()
        report = self.reports()[0]
        self.assertEqual({tab["id"]: tab["count"] for tab in catalog["tabs"]},
                         {"generate": 1, "generating": 0, "post": 0, "ellie": 1, "drive": 1, "done": 0, "input": 1})
        self.assertEqual([tab.split("\n")[0] for tab in sections(report["blocks"])],
                         [f"*{tab['label']}* ({tab['count']})" for tab in catalog["tabs"]])
        spend = catalog["spend"]
        self.assertEqual(spend["images"], 8)
        self.assertEqual(report["blocks"][1]["text"]["text"],
                         f"Generation spend (estimate): {spend['images']} images · about ${spend['est_cost_usd']:.2f}")
        self.assertIn("• VASE-042 · Ceramic vase", sections(report["blocks"])[4])
        self.assertIn("LAMP-7> · Ceramic vase", sections(report["blocks"])[3])


if __name__ == "__main__":
    unittest.main()
