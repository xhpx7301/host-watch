import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin

NOW = 1792382400.0


def listing(**changes):
    row = dict(id="288702", name="Test", plan_name="Plan", area_name="德国", node_name="DE 9929",
               price="2000", server_price=3000, server_cycle=1, due_time=NOW + 30 * 86400,
               status=0, is_bid=False, cpu=1, memory=1024, disk=10)
    return {**row, **changes}


def rule(**changes):
    return {**dict(id="test", target="listing", product_id="288702", name="Test", plan_name="Plan",
                   region="德国", node="DE 9929", direction="discount", rate_threshold=10,
                   amount_threshold=None, match="any", action="notify", enabled=True), **changes}


def config(rules=None):
    value = admin.default_config()
    value["vps_seckill"]["accounts"] = [dict(id="ak", name="Akile", provider="akile", username="test@example.com", password="private")]
    value["akile_market"] = dict(enabled=True, interval_seconds=60, account_id="ak", items=rules or [rule()])
    value["telegram"] = dict(bot_token="secret-token", chat_id="123", disabled=False)
    return admin.validate_config(value)


def response(payload):
    return Mock(status_code=200, json=Mock(return_value=payload))


class ValuationTests(unittest.TestCase):
    def test_monthly_annual_and_multi_month_amounts(self):
        item = admin.akile_market_valuation(listing(), NOW)
        self.assertEqual(item["fair_value"], 30)
        self.assertEqual(item["delta"], 10)
        self.assertAlmostEqual(item["rate"], 100 / 3)
        short_remaining = admin.akile_market_valuation(listing(due_time=NOW + 5 * 86400), NOW)
        self.assertEqual(short_remaining["fair_value"], 30)
        self.assertEqual(short_remaining["delta"], 10)
        year = admin.akile_market_valuation(listing(server_cycle=12, server_price=12000, due_time=NOW + 365 * 86400), NOW)
        self.assertEqual(year["fair_value"], 10)
        quarter = admin.akile_market_valuation(listing(server_cycle=3, due_time=NOW + 90 * 86400), NOW)
        self.assertEqual(quarter["fair_value"], 10)

    def test_unsupported_zero_expired_and_nonfinite_never_trigger(self):
        for changes in [dict(server_cycle=2), dict(server_price=0), dict(due_time=NOW), dict(is_bid=True), dict(status=1), dict(status="0")]:
            with self.subTest(changes=changes):
                item = admin.akile_market_valuation(listing(**changes), NOW)
                self.assertFalse(admin.akile_market_rule_matches(rule(rate_threshold=0), item))
        for key in ["price", "server_price", "due_time"]:
            for val in [None, "NaN", "Infinity", True, -1]:
                with self.assertRaises(ValueError):
                    admin.akile_market_valuation(listing(**{key:val}), NOW)

    def test_discount_minimum_premium_maximum_and_or(self):
        cheap = admin.akile_market_valuation(listing(), NOW)
        expensive = admin.akile_market_valuation(listing(price=3300), NOW)
        self.assertTrue(admin.akile_market_rule_matches(rule(amount_threshold=10, rate_threshold=None), cheap))
        self.assertFalse(admin.akile_market_rule_matches(rule(amount_threshold=11, rate_threshold=40), cheap))
        self.assertTrue(admin.akile_market_rule_matches(rule(amount_threshold=11, rate_threshold=30), cheap))
        self.assertFalse(admin.akile_market_rule_matches(rule(amount_threshold=11, rate_threshold=30, match="all"), cheap))
        self.assertTrue(admin.akile_market_rule_matches(rule(direction="premium", rate_threshold=10), expensive))
        self.assertFalse(admin.akile_market_rule_matches(rule(direction="premium", rate_threshold=9), expensive))
        self.assertTrue(admin.akile_market_rule_matches(rule(direction="premium", rate_threshold=0), cheap))

    def test_listing_vs_exact_plan_region_node(self):
        item = admin.akile_market_valuation(listing(id="999"), NOW)
        self.assertFalse(admin.akile_market_rule_matches(rule(), item))
        self.assertTrue(admin.akile_market_rule_matches(rule(target="plan"), item))
        for key in ["plan_name", "node", "region"]:
            self.assertFalse(admin.akile_market_rule_matches(rule(target="plan", **{key:"other"}), item))

    def test_config_rejects_bad_thresholds_accounts_and_empty_conditions(self):
        for row in [rule(rate_threshold=float("nan")), rule(rate_threshold=-1), rule(rate_threshold=True),
                    rule(rate_threshold=None), rule(target="plan", node=""), rule(product_id="../other")]:
            with self.assertRaises(ValueError):
                config([row])
        value=config();value["akile_market"]["account_id"]="missing"
        with self.assertRaises(ValueError): admin.validate_config(value)
        value=config();value["akile_market"]["items"][0]["rate_threshold"]=0
        self.assertEqual(admin.validate_config(value)["akile_market"]["items"][0]["rate_threshold"],0)
        public=admin.public_config(config())
        self.assertNotIn("secret-token",json.dumps(public))
        self.assertNotIn('"private"',json.dumps(public))

    def test_market_info_is_optional_validated_and_preserved(self):
        old_rule = config()["akile_market"]["items"][0]
        self.assertNotIn("market_info", old_rule)
        saved = config([rule(market_info={"cpu": 4, "memory": "8192 MB", "disk": 80,
            "bandwidth": "500 Mbps", "flow": "1 TB/月", "renewal": 120, "cycle": 12})])
        info = saved["akile_market"]["items"][0]["market_info"]
        self.assertEqual(info["cpu"], "4")
        self.assertEqual(info["renewal"], 120.0)
        self.assertEqual(info["cycle"], "12")
        for market_info in [{"renewal": float("inf")}, {"cpu": "x" * 81}, {"cycle": []}, []]:
            with self.subTest(market_info=market_info), self.assertRaises(ValueError):
                config([rule(market_info=market_info)])

    def test_pagination_preserves_fixed_size_and_rejects_incomplete_results(self):
        session=Mock()
        session.post.side_effect=[response(dict(status_code=0,list=[listing()],total=2)),
                                  response(dict(status_code=0,list=[listing(id="999")],total=2))]
        self.assertEqual(len(admin.akile_market_list(session,page_size=1)),2)
        self.assertEqual([call.kwargs["json"]["page_num"] for call in session.post.call_args_list],[1,2])
        session.post.side_effect=[response(dict(status_code=0,list=[listing()],total=2)),response(dict(status_code=0,list=None,total=2))]
        with self.assertRaises(ValueError): admin.akile_market_list(session,page_size=1)
        session.post.side_effect=[response(dict(status_code=0,list=[listing()],total=2)),response(dict(status_code=0,list=[listing()],total=2))]
        with self.assertRaises(ValueError): admin.akile_market_list(session,page_size=1)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"config.json"
        patcher=patch.object(admin,"CONFIG_PATH",self.path);patcher.start();self.addCleanup(patcher.stop)
        self.item=admin.akile_market_valuation(listing(),NOW)
        self.session=Mock()

    def evaluate(self,value=None,items=None):
        return admin._evaluate_akile_market(value or config(),[self.item] if items is None else items,self.session)

    def test_restart_deduplication_rearm_and_rule_changes(self):
        with patch.object(admin,"telegram_call") as send:
            self.evaluate();self.evaluate()
            self.assertEqual(send.call_count,1)
            miss=admin.akile_market_valuation(listing(price=4000),NOW)
            self.evaluate(items=[miss]);self.evaluate()
            self.assertEqual(send.call_count,2)
            self.evaluate(config([rule(amount_threshold=1)]))
            self.assertEqual(send.call_count,3)
        self.session.post.assert_not_called()
        state=json.loads((self.path.parent/"state-akile-market.json").read_text(encoding="utf-8"))
        self.assertNotIn("secret-token",json.dumps(state))

    def test_delivery_failure_and_disabled_telegram_retry_without_losing_trigger(self):
        with patch.object(admin,"telegram_call",side_effect=[admin.TelegramAPIError("failed"),{}]) as send:
            self.evaluate();self.evaluate();self.evaluate()
            self.assertEqual(send.call_count,2)
        value=config();value["telegram"]["disabled"]=True
        with patch.object(admin,"telegram_call") as send:
            self.evaluate(value);send.assert_not_called()
            self.evaluate();send.assert_not_called()
            paused=config([rule(id="paused-new")]);paused["telegram"]["disabled"]=True
            self.evaluate(paused);send.assert_not_called()
            paused["telegram"]["disabled"]=False
            self.evaluate(paused);self.assertEqual(send.call_count,1)

    def test_passkey_blocked_mode_never_submits_order_or_payment(self):
        with patch.object(admin,"telegram_call") as send:
            result=self.evaluate(config([rule(action="unpaid_order")]))
            self.evaluate(config([rule(action="unpaid_order")]))
        self.assertEqual(send.call_count,1)
        self.assertEqual(result[0]["status"],"verification_required")
        self.assertIn("未提交订单",result[0]["message"])
        self.session.post.assert_not_called();self.session.get.assert_not_called()

    def test_corrupt_state_fails_closed(self):
        (self.path.parent/"state-akile-market.json").write_text("invalid")
        with patch.object(admin,"telegram_call") as send, self.assertRaises(ValueError): self.evaluate()
        send.assert_not_called()

    def test_browse_is_read_only_and_refresh_failure_retains_snapshot(self):
        value=config(); session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        with patch.object(admin,"current_config",return_value=value), patch.object(admin,"_akile_market_session",return_value=session), patch.object(admin,"akile_market_list",return_value=[listing()]), patch.object(admin,"_evaluate_akile_market") as evaluate, patch.dict(admin.AKILE_MARKET_CACHE,{},clear=True):
            result=admin.refresh_akile_market()
            self.assertEqual(len(result["items"]),1);evaluate.assert_not_called()
            self.assertFalse((self.path.parent/"state-akile-market.json").exists())
            with patch.object(admin,"akile_market_list",side_effect=ValueError("failed")),self.assertRaises(ValueError):admin.refresh_akile_market(evaluate=True)
            self.assertEqual(len(admin._akile_market_snapshot()["items"]),1)
            evaluate.assert_not_called()

    def test_overlapping_refresh_is_rejected(self):
        with admin.AKILE_MARKET_RUN_LOCK, self.assertRaises(ValueError): admin.refresh_akile_market()


if __name__=="__main__": unittest.main()
