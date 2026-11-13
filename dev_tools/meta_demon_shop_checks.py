import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from module.exception import TaskEnd
from tasks.MetaDemonShop.config import MetaDemonShop
from tasks.MetaDemonShop.script_task import BOX_PRICE, ScriptTask, parse_amount


class ParseAmountChecks(unittest.TestCase):
    def test_plain_and_wan_amounts(self):
        self.assertEqual(parse_amount('1887'), 1887)
        self.assertEqual(parse_amount('886万'), 8860000)
        self.assertEqual(parse_amount('1.68万'), 16800)
        self.assertEqual(parse_amount(' 2,177万 '), 21770000)

    def test_lookalike_unit_is_still_wan(self):
        self.assertEqual(parse_amount('886方'), 8860000)

    def test_unreadable_amounts_are_rejected(self):
        for text in ('', None, '万', '1.68', '886万1', '累计'):
            self.assertIsNone(parse_amount(text), text)


class ConfigChecks(unittest.TestCase):
    def test_defaults_rerun_two_minutes_after_a_purchase(self):
        conf = MetaDemonShop()
        self.assertEqual(conf.scheduler.success_interval, timedelta(minutes=2))
        self.assertEqual(conf.meta_demon_shop_config.buy_times, 20)
        self.assertTrue(conf.meta_demon_shop_config.call_realm_raid)


def shop_task(currencies, buy_times=20, realm_raid_enabled=True):
    """不连设备的任务对象：红叶读数按顺序给出，购买弹窗总能打开。"""
    task = ScriptTask.__new__(ScriptTask)
    task.config = SimpleNamespace(
        meta_demon_shop=MetaDemonShop(meta_demon_shop_config={'buy_times': buy_times}),
        realm_raid=SimpleNamespace(scheduler=SimpleNamespace(enable=realm_raid_enabled)),
    )
    task.goto_page = MagicMock()
    task.close_buy_dialog = MagicMock()
    task.set_next_run = MagicMock()
    task.read_currency = MagicMock(side_effect=currencies)
    task.open_buy_dialog = MagicMock(return_value=True)
    task.confirm_purchase = MagicMock(return_value=True)
    return task


def next_runs(task):
    return {call.kwargs['task']: call.kwargs for call in task.set_next_run.call_args_list}


class RunChecks(unittest.TestCase):
    def test_not_enough_currency_ends_without_buying(self):
        for currency in (BOX_PRICE - 1, None):
            task = shop_task([currency])
            with self.assertRaises(TaskEnd):
                task.run()
            task.open_buy_dialog.assert_not_called()
            runs = next_runs(task)
            self.assertNotIn('RealmRaid', runs)
            self.assertFalse(runs['MetaDemonShop']['success'])

    def test_buys_the_configured_times_then_calls_realm_raid(self):
        task = shop_task([8860000] * 4, buy_times=3)
        with self.assertRaises(TaskEnd):
            task.run()
        self.assertEqual(task.confirm_purchase.call_count, 3)
        runs = next_runs(task)
        self.assertIn('target', runs['RealmRaid'])
        self.assertTrue(runs['MetaDemonShop']['success'])

    def test_stops_when_currency_runs_out_midway(self):
        task = shop_task([5000, 5000, 3112, 1224], buy_times=20)
        with self.assertRaises(TaskEnd):
            task.run()
        self.assertEqual(task.confirm_purchase.call_count, 2)
        self.assertTrue(next_runs(task)['MetaDemonShop']['success'])

    def test_failed_purchase_is_not_counted(self):
        task = shop_task([8860000] * 2)
        task.confirm_purchase.return_value = False
        with self.assertRaises(TaskEnd):
            task.run()
        runs = next_runs(task)
        self.assertNotIn('RealmRaid', runs)
        self.assertFalse(runs['MetaDemonShop']['success'])


class FindBoxChecks(unittest.TestCase):
    @staticmethod
    def result(text, x, y):
        box = np.array([[x, y], [x + 154, y], [x + 154, y + 23], [x, y + 23]], dtype=float)
        return SimpleNamespace(ocr_text=text, box=box)

    def find(self, results):
        task = ScriptTask.__new__(ScriptTask)
        task.device = SimpleNamespace(image=None)
        with patch.object(ScriptTask.O_SHOP_ITEMS, 'detect_and_ocr', return_value=results):
            return task.find_box()

    def test_clicks_randomly_inside_the_icon_under_the_simple_box_name(self):
        target = self.find([self.result('超鬼王朴素礼盒', 239, 218), self.result('超鬼王简易礼盒', 22, 18)])
        # 截图里简易礼盒的图标大约在 x 565-640、y 270-345
        self.assertEqual(tuple(target.roi_front), (576, 281, 56, 54))
        points = {target.coord() for _ in range(50)}
        self.assertGreater(len(points), 1)
        for x, y in points:
            self.assertTrue(576 <= x <= 632 and 281 <= y <= 335, (x, y))

    def test_plain_box_alone_is_never_a_match(self):
        self.assertIsNone(self.find([self.result('超鬼王朴素礼盒', 239, 218)]))

    def test_half_visible_card_waits_for_another_swipe(self):
        self.assertIsNone(self.find([self.result('超鬼王简易礼盒', 22, 300)]))


if __name__ == '__main__':
    unittest.main()
