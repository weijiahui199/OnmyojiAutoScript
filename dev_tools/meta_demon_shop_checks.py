import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from module.exception import TaskEnd
from tasks.MetaDemonShop.config import BoxType, MetaDemonShop
from tasks.MetaDemonShop.script_task import ScriptTask, parse_amount


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
        self.assertEqual(conf.meta_demon_shop_config.buy_times, 8)
        self.assertTrue(conf.meta_demon_shop_config.call_realm_raid)

    def test_default_box_is_the_8888_one_and_boxes_are_told_apart_by_name(self):
        self.assertEqual(MetaDemonShop().meta_demon_shop_config.box, BoxType.FINE)
        self.assertEqual([(box.keyword, box.price) for box in BoxType],
                         [('简易', 1888), ('朴素', 4888), ('精致', 8888)])
        names = ['超鬼王简易礼盒', '超鬼王朴素礼盒', '超鬼王精致礼盒']
        for box in BoxType:
            self.assertEqual([name for name in names if box.keyword in name], [f'超鬼王{box.label}'])


def shop_task(currencies, buy_times=20, realm_raid_enabled=True, tickets_full=False, call_realm_raid=True,
              box=BoxType.SIMPLE):
    """不连设备的任务对象：红叶读数按顺序给出，购买弹窗总能打开。"""
    task = ScriptTask.__new__(ScriptTask)
    task.config = SimpleNamespace(
        meta_demon_shop=MetaDemonShop(meta_demon_shop_config={
            'buy_times': buy_times, 'call_realm_raid': call_realm_raid, 'box': box.value}),
        realm_raid=SimpleNamespace(scheduler=SimpleNamespace(enable=realm_raid_enabled)),
    )
    task.goto_page = MagicMock()
    task.realm_raid_tickets_full = MagicMock(return_value=tickets_full)
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
        for box, currency in ((BoxType.SIMPLE, 1887), (BoxType.FINE, 8887), (BoxType.FINE, None)):
            task = shop_task([currency], box=box)
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

    def test_fine_box_needs_8888_per_purchase(self):
        # 2 万红叶：精致礼盒买得起两次（每次读数后各扣 8888），第三次不够
        task = shop_task([20000, 20000, 11112, 2224], box=BoxType.FINE)
        with self.assertRaises(TaskEnd):
            task.run()
        self.assertEqual(task.confirm_purchase.call_count, 2)

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


class RealmRaidTicketChecks(unittest.TestCase):
    def test_full_tickets_skip_buying_and_run_realm_raid_first(self):
        task = shop_task([], tickets_full=True)
        with self.assertRaises(TaskEnd):
            task.run()
        task.read_currency.assert_not_called()
        task.open_buy_dialog.assert_not_called()
        runs = next_runs(task)
        self.assertIn('target', runs['RealmRaid'])
        # 很快再来：个人突破把券打掉之后接着买
        self.assertTrue(runs['MetaDemonShop']['success'])

    def test_full_tickets_without_a_way_to_spend_them_back_off(self):
        for options in ({'realm_raid_enabled': False}, {'call_realm_raid': False}):
            with self.subTest(options):
                task = shop_task([], tickets_full=True, **options)
                with self.assertRaises(TaskEnd):
                    task.run()
                task.open_buy_dialog.assert_not_called()
                runs = next_runs(task)
                self.assertNotIn('RealmRaid', runs)
                self.assertFalse(runs['MetaDemonShop']['success'])

    def read(self, *counters):
        task = ScriptTask.__new__(ScriptTask)
        task.goto_page = MagicMock()
        task.screenshot = MagicMock()
        task.device = SimpleNamespace(image=None)
        rule = 'tasks.MetaDemonShop.script_task.ExplorationAssets.O_REALM_RAID_NUMBER_META'
        with patch(rule) as ocr, patch('tasks.MetaDemonShop.script_task.time.sleep'):
            ocr.ocr_digit_counter.side_effect = counters
            return task.realm_raid_tickets_full()

    def test_ticket_counter_decides_full(self):
        self.assertTrue(self.read((30, 0, 30)))
        self.assertFalse(self.read((29, 1, 30)))
        self.assertFalse(self.read((0, 30, 30)))

    def test_invalid_readings_are_retried_then_treated_as_not_full(self):
        # 第一帧读错（上限不是 30），第二帧读对
        self.assertTrue(self.read((3, 0, 3), (30, 0, 30)))
        # 一直读不出来：不拦着购买
        self.assertFalse(self.read((0, 0, 0), (0, 0, 0), (41, -11, 30)))


class NavigationChecks(unittest.TestCase):
    """导航只在「全局 + 起点分类 + 终点分类 + 任务分类」里找路，商店必须和超鬼王同一分类。"""

    @staticmethod
    def task():
        from tasks.GameUi.registry import PageRegistry
        from tasks.GameUi.session import NavigatorSession

        task = ScriptTask.__new__(ScriptTask)
        task.navigator = NavigatorSession(task_category=task._infer_task_category())
        task.navigator.bootstrap(PageRegistry.all())
        return task

    def path(self, start, destination):
        task = self.task()
        current = task.navigator.resolve_page(start)
        route = task._build_path(current, task.navigator.resolve_page(destination))
        self.assertIsNotNone(route, f'{start} -> {destination} 没有路')
        return [transition.destination.name for transition in route]

    def test_courtyard_reaches_the_shop_through_meta_demon(self):
        from tasks.GameUi.page import page_main
        from tasks.MetaDemonShop.page import page_meta_demon_shop

        self.assertEqual(self.path(page_main, page_meta_demon_shop), ['page_meta_demon', 'page_meta_demon_shop'])

    def test_exploration_page_reaches_the_shop_after_the_ticket_check(self):
        from tasks.GameUi.page import page_exploration
        from tasks.MetaDemonShop.page import page_meta_demon_shop

        self.assertEqual(self.path(page_exploration, page_meta_demon_shop),
                         ['page_main', 'page_meta_demon', 'page_meta_demon_shop'])

    def test_shop_returns_to_the_courtyard(self):
        from tasks.GameUi.page import page_main
        from tasks.MetaDemonShop.page import page_meta_demon_shop

        self.assertEqual(self.path(page_meta_demon_shop, page_main), ['page_meta_demon', 'page_main'])

    def test_task_detects_meta_demon_pages(self):
        from tasks.MetaDemon.page import page_meta_demon

        self.assertEqual(self.task()._infer_task_category(), page_meta_demon.category)


class FindBoxChecks(unittest.TestCase):
    @staticmethod
    def result(text, x, y):
        box = np.array([[x, y], [x + 154, y], [x + 154, y + 23], [x, y + 23]], dtype=float)
        return SimpleNamespace(ocr_text=text, box=box)

    def find(self, results, box=BoxType.SIMPLE):
        task = ScriptTask.__new__(ScriptTask)
        task.box = box
        task.device = SimpleNamespace(image=None)
        with patch.object(ScriptTask.O_SHOP_ITEMS, 'detect_and_ocr', return_value=results):
            return task.find_box()

    def test_clicks_randomly_on_the_quantity_and_price_rows_not_the_icon(self):
        target = self.find([self.result('超鬼王朴素礼盒', 239, 218), self.result('超鬼王简易礼盒', 22, 18)])
        # 名字顶边在 y=243 时：图标约 y 270-345（点了只弹说明），数量和价格两行约 y 353-399
        self.assertEqual(tuple(target.roi_front), (554, 351, 100, 42))
        points = {target.coord() for _ in range(50)}
        self.assertGreater(len(points), 1)
        for x, y in points:
            self.assertTrue(554 <= x <= 654 and 351 <= y <= 393, (x, y))

    def test_each_box_type_only_matches_its_own_card(self):
        cards = [self.result('超鬼王简易礼盒', 22, 18), self.result('超鬼王精致礼盒', 239, 18),
                 self.result('超鬼王朴素礼盒', 456, 18)]
        centers = {box: self.find(cards, box).roi_front[0] + 50 for box in BoxType}
        self.assertEqual(centers, {BoxType.SIMPLE: 505 + 99, BoxType.FINE: 505 + 316, BoxType.PLAIN: 505 + 533})

    def test_plain_box_alone_is_never_a_match(self):
        self.assertIsNone(self.find([self.result('超鬼王朴素礼盒', 239, 218)]))

    def test_half_visible_card_waits_for_another_swipe(self):
        self.assertIsNone(self.find([self.result('超鬼王简易礼盒', 22, 300)]))


if __name__ == '__main__':
    unittest.main()
