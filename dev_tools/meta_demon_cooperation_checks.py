"""超鬼王协同通道与双任务身份校验（含独立门票任务）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from module.cooperation import Cooperation
from module.exception import ScriptError
from tasks.MetaDemon.assistance import AssistanceLink, GhostAssistant
from tasks.MetaDemon.config import AssistanceRole, MetaDemon, MetaDemonMode, OfferingMode


def write_config(root, name, *, role, peer='', enable=True, mode=None, ticket=None, **assistance):
    config = {
        'meta_demon': {
            'scheduler': {'enable': enable, 'next_run': '2026-01-01 00:00:00'},
            'meta_demon_config': {'mode': [m.value for m in (mode or [MetaDemonMode.BOSS_ASSIST])]},
            'assistance_config': {'role': role.value, 'cooperation_script': peer, **assistance},
        }
    }
    if ticket:
        config['meta_demon_ticket'] = ticket
    folder = Path(root) / 'config'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')


def ticket_config(user_status, *, peer='', enable=True, mode='门票刷取：觉醒'):
    return {
        'scheduler': {'enable': enable, 'next_run': '2026-01-01 00:00:00'},
        'general_config': {'mode': mode, 'user_status': user_status},
        'invite_config': {'cooperation_script': peer},
    }


class CooperationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_config(self.root, 'offer', role=AssistanceRole.OFFERING, peer='assist')
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer')

    def runtime(self, name='offer'):
        return Cooperation(name, root=self.root)

    def test_battle_state_round_trip_through_sqlite(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        a.publish_battle_state('MetaDemonAssistance', 'assist', 'session-1', {'portrait': 'png-base64'})
        state = b.peer_battle_state('MetaDemonAssistance', 'offer')
        self.assertEqual(state['session'], 'session-1')
        self.assertEqual(state['payload']['portrait'], 'png-base64')
        self.assertIsNotNone(state['updated'])
        a.publish_battle_state('MetaDemonAssistance', 'assist', 'session-2', {'expires': 1})
        self.assertEqual(b.peer_battle_state('MetaDemonAssistance', 'offer')['session'], 'session-2')
        self.assertIsNone(b.peer_battle_state('MetaDemonAssistance', 'missing'))

    def test_meta_demon_pair_requires_opposite_roles_and_assist_mode(self):
        a = self.runtime('offer')
        self.assertIsNotNone(a.validate('MetaDemon', 'assist'))
        write_config(self.root, 'assist', role=AssistanceRole.OFFERING, peer='offer')
        with self.assertRaises(ScriptError):
            a.validate('MetaDemon', 'assist')
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer',
                     mode=[MetaDemonMode.BOSS_BATTLE])
        with self.assertRaises(ScriptError):
            a.validate('MetaDemon', 'assist')

    def test_independent_offer_cannot_be_used_as_a_cooperative_partner(self):
        path = self.root / 'config' / 'offer.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['meta_demon']['assistance_config']['offering_mode'] = OfferingMode.INDEPENDENT
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(ScriptError, '不协同'):
            self.runtime('assist').validate('MetaDemon', 'offer')

    def test_running_assistant_detects_offer_switching_to_independent_mode(self):
        offer = self.runtime('offer')
        offer.publish('MetaDemon')
        task = SimpleNamespace(
            conf=MetaDemon(assistance_config={'role': '协助', 'cooperation_script': 'offer'}),
            config=SimpleNamespace(config_name='assist', cooperation_runtime=self.runtime('assist')),
        )
        link = AssistanceLink(task)
        self.assertTrue(link.peer_in_task())
        path = self.root / 'config' / 'offer.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['meta_demon']['assistance_config']['offering_mode'] = OfferingMode.INDEPENDENT
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.assertFalse(link.peer_in_task())

    def test_ticket_pair_requires_leader_member_and_same_mode(self):
        write_config(self.root, 'leader', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='member'))
        write_config(self.root, 'member', role=AssistanceRole.OFFERING, ticket=ticket_config('member', peer='leader'))
        leader = self.runtime('leader')
        self.assertTrue(leader.matches_meta_demon_tickets('member'))
        self.assertIsNotNone(leader.validate('MetaDemonTicket', 'member'))
        # 两个队长或玩法不同都不算合法配对。
        write_config(self.root, 'member', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='leader'))
        self.assertFalse(leader.matches_meta_demon_tickets('member'))
        with self.assertRaises(ScriptError):
            leader.validate('MetaDemonTicket', 'member')
        write_config(self.root, 'member', role=AssistanceRole.OFFERING,
                     ticket=ticket_config('member', peer='leader', mode='门票刷取：探索'))
        self.assertFalse(leader.matches_meta_demon_tickets('member'))

    def test_ticket_pair_requires_enabled_schedulers(self):
        write_config(self.root, 'leader', role=AssistanceRole.OFFERING, ticket=ticket_config('leader', peer='member'))
        write_config(self.root, 'member', role=AssistanceRole.OFFERING,
                     ticket=ticket_config('member', peer='leader', enable=False))
        self.assertFalse(self.runtime('leader').matches_meta_demon_tickets('member'))

    def test_legacy_meta_demon_reads_are_split_on_demand(self):
        legacy = {
            'meta_demon': {
                'scheduler': {'enable': True, 'next_run': '2026-01-01 00:00:00'},
                'meta_demon_config': {'mode': ['门票刷取：探索']},
            }
        }
        folder = self.root / 'config'
        (folder / 'legacy.json').write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
        data = self.runtime('legacy').read('legacy')
        self.assertEqual(data['meta_demon']['meta_demon_config']['mode'], ['鬼王战斗'])
        self.assertEqual(data['meta_demon_ticket']['general_config']['mode'], '门票刷取：探索')

    def test_prepare_reports_skip_when_peer_is_elsewhere(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        b.publish('ActivityShikigami')
        config = SimpleNamespace(task_delay=Mock())
        with patch('module.cooperation.time.sleep') as sleep:
            self.assertFalse(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        sleep.assert_not_called()
        config.task_delay.assert_not_called()

    def test_prepare_detects_ready_peer(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        b.publish('MetaDemon')
        config = SimpleNamespace(task_delay=Mock())
        with patch('module.cooperation.time.sleep') as sleep:
            self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        sleep.assert_not_called()

    def test_prepare_rechecks_missing_and_expired_heartbeats_for_thirty_seconds(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        for expired in (False, True):
            with self.subTest(expired=expired):
                if expired:
                    b.publish('MetaDemon')
                    with b.connect() as db:
                        db.execute('UPDATE runners SET heartbeat=heartbeat-60 WHERE name=?', ('assist',))
                with patch('module.cooperation.time.monotonic', side_effect=(0, 0, 5, 10, 15, 20, 25, 30)), \
                        patch('module.cooperation.time.sleep') as sleep:
                    self.assertFalse(a.prepare(config, 'MetaDemon', 'assist', defer=False))
                self.assertEqual(sleep.call_args_list, [call(5)] * 6)
                self.assertEqual(b.requested(), [])
        config.task_delay.assert_not_called()

    def test_prepare_continues_when_peer_heartbeat_arrives_during_retry(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        for peer_task in ('', 'MetaDemon'):
            with self.subTest(peer_task=peer_task):
                with b.connect() as db:
                    db.execute('DELETE FROM runners WHERE name=?', ('assist',))
                with patch('module.cooperation.time.monotonic', return_value=0), \
                        patch('module.cooperation.time.sleep', side_effect=lambda seconds: b.publish(peer_task)) as sleep:
                    self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
                sleep.assert_called_once_with(5)
                if peer_task == '':
                    self.assertEqual(b.requested(), [('MetaDemon', 'offer')])
                else:
                    self.assertEqual(b.requested(), [])

    def test_prepare_accepts_peer_heartbeat_on_last_retry(self):
        a, b = self.runtime('offer'), self.runtime('assist')
        config = SimpleNamespace(task_delay=Mock())
        elapsed = 0

        def advance_time(seconds):
            nonlocal elapsed
            elapsed += seconds
            if elapsed == 30:
                b.publish('MetaDemon')

        with patch('module.cooperation.time.monotonic', side_effect=lambda: elapsed), \
                patch('module.cooperation.time.sleep', side_effect=advance_time) as sleep:
            self.assertTrue(a.prepare(config, 'MetaDemon', 'assist', defer=False))
        self.assertEqual(sleep.call_args_list, [call(5)] * 6)


class AssistanceLinkChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_task(self, *, role, peer, self_name='offer'):
        from datetime import datetime, timedelta
        conf = MetaDemon(assistance_config={'role': role.value, 'cooperation_script': peer,
                                           'friend_names': 'Alice'})
        config = SimpleNamespace(config_name=self_name, cooperation_runtime=Cooperation(self_name, root=self.root))
        return SimpleNamespace(conf=conf, config=config, start_time=datetime.now(),
                               limit_time=timedelta(minutes=30))

    def test_peer_with_same_role_is_rejected(self):
        write_config(self.root, 'assist', role=AssistanceRole.OFFERING, peer='offer')
        task = self.make_task(role=AssistanceRole.OFFERING, peer='assist')
        with self.assertRaises(ScriptError):
            AssistanceLink(task)

    def test_pair_accepts_opposite_roles(self):
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer')
        task = self.make_task(role=AssistanceRole.OFFERING, peer='assist')
        link = AssistanceLink(task)
        self.assertEqual(link.peer, 'assist')
        link.publish('portrait-b64')
        peer = Cooperation('assist', root=self.root).peer_battle_state(link.CHANNEL, 'offer')
        self.assertIsNotNone(peer)
        self.assertEqual(peer['payload']['portrait'], 'portrait-b64')
        self.assertFalse(link.confirmed())

    def test_assist_serves_multiple_offering_peers_in_turn(self):
        for name in ('offer_a', 'offer_b'):
            write_config(self.root, name, role=AssistanceRole.OFFERING, peer='assist')
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST)
        task = self.make_task(role=AssistanceRole.ASSIST, peer='', self_name='assist')
        link = AssistanceLink(task)
        self.assertEqual(link.peers, ['offer_a', 'offer_b'])
        self.assertIsNone(link.target())

        def offer(name, portrait, star):
            offer_task = self.make_task(role=AssistanceRole.OFFERING, peer='assist', self_name=name)
            offer_link = AssistanceLink(offer_task)
            offer_link.publish(portrait, star=star)
            return offer_link

        link_b = offer('offer_b', 'portrait-b', 5)
        link_a = offer('offer_a', 'portrait-a', 4)
        # 最早分享的上供端优先，且处理期间保持不变。
        first = link.target()
        self.assertEqual((first[2], first[3]), ('portrait-b', 'offer_b'))
        self.assertEqual(int(link.target_star(first)), 5)
        self.assertEqual(link.target(), first)
        link.confirm(first)
        self.assertTrue(link_b.confirmed())
        self.assertFalse(link_a.confirmed())
        # 结算后切到下一个上供端。
        link.confirm(first, settled=True)
        self.assertTrue(link_b.peer_settled())
        second = link.target()
        self.assertEqual((second[2], second[3]), ('portrait-a', 'offer_a'))
        self.assertEqual(int(link.target_star(second)), 4)

    def relay_task(self, *, done=True, elapsed=0, count=0):
        from datetime import datetime, timedelta
        return SimpleNamespace(
            config=SimpleNamespace(config_name='a'), State=SimpleNamespace(done=done),
            start_time=datetime.now() - timedelta(minutes=elapsed), limit_time=timedelta(minutes=30),
            total_count=count, limit_count=50, set_next_run=Mock(), finish_task=Mock())

    def test_relay_wakes_next_script_only_when_tickets_are_used_up(self):
        from tasks.MetaDemon.assistance import _finish_relay
        with patch('tasks.MetaDemon.assistance.wake_script') as wake:
            task = self.relay_task()
            _finish_relay(task, 'b', True)
            wake.assert_called_once_with('b')
            task.set_next_run.assert_called_once_with(task='MetaDemon', success=True)
            # 到运行时间上限但门票没用完：不唤醒，马上继续。
            wake.reset_mock()
            task = self.relay_task(done=False, elapsed=31)
            _finish_relay(task, 'b', True)
            wake.assert_not_called()
            self.assertIn('target', task.set_next_run.call_args.kwargs)
            # 失败：不唤醒，几分钟后重试。
            task = self.relay_task()
            _finish_relay(task, 'b', False)
            wake.assert_not_called()
            self.assertIn('target', task.set_next_run.call_args.kwargs)
            # 下一个是自己：不唤醒。
            task = self.relay_task()
            _finish_relay(task, 'a', True)
            wake.assert_not_called()

    def test_next_offering_script_is_normalized(self):
        from tasks.MetaDemon.config import MetaDemonAssistanceConfig
        self.assertEqual(MetaDemonAssistanceConfig(next_offering_script=' b ').next_offering_script, 'b')
        self.assertEqual(MetaDemonAssistanceConfig().next_offering_script, '')

    def test_offerer_waits_while_assist_rests_for_fatigue(self):
        from datetime import datetime, timedelta
        from module.exception import TaskEnd
        from tasks.MetaDemon.assistance import _defer_while_peer_rests
        write_config(self.root, 'assist', role=AssistanceRole.ASSIST, peer='offer')
        write_config(self.root, 'offer', role=AssistanceRole.OFFERING, peer='assist')
        assist = AssistanceLink(self.make_task(role=AssistanceRole.ASSIST, peer='offer', self_name='assist'))
        offer = AssistanceLink(self.make_task(role=AssistanceRole.OFFERING, peer='assist'))
        self.assertIsNone(offer.peer_rest_until())
        until = datetime.now().replace(microsecond=0) + timedelta(minutes=100)
        assist.mark_rest(until)
        self.assertEqual(offer.peer_rest_until(), until)
        task = SimpleNamespace(set_next_run=Mock(), finish_task=Mock())
        with self.assertRaises(TaskEnd):
            _defer_while_peer_rests(task, offer)
        task.set_next_run.assert_called_once_with(task='MetaDemon', target=until + timedelta(minutes=1))
        # 协助端回到超鬼王后清除；过期的记录也不再拦截。
        assist.clear_rest()
        self.assertIsNone(offer.peer_rest_until())
        assist.mark_rest(datetime.now() - timedelta(minutes=1))
        self.assertIsNone(offer.peer_rest_until())
        _defer_while_peer_rests(task, offer)

    def test_offering_star_order_is_custom(self):
        from tasks.MetaDemon.config import BossType, MetaDemonAssistanceConfig, OfferingStar
        B = BossType
        self.assertEqual(OfferingStar.FOUR_PLUS.stars, (B.FIVE_STARS, B.FOUR_STARS, B.SIX_STARS))
        self.assertEqual(OfferingStar.SIX.stars, (B.SIX_STARS,))
        cases = {'6,5,4': '6,5,4', '4，5；6': '4,5,6', '6 6 5': '6,5', '六星': '6',
                 '四星及以上': '5,4,6', '': '6', '1,2,3': '6'}
        for raw, expected in cases.items():
            conf = MetaDemonAssistanceConfig(offering_star=raw)
            self.assertEqual(conf.offering_star, expected, raw)
        self.assertEqual(MetaDemonAssistanceConfig(offering_star='6,5,4').offering_stars,
                         (B.SIX_STARS, B.FIVE_STARS, B.FOUR_STARS))


class GhostAssistantChecks(unittest.TestCase):
    def make_task(self):
        from tasks.MetaDemon.assets import MetaDemonAssets
        return SimpleNamespace(goto_page=Mock(), screenshot=Mock(), appear=Mock(),
                               device=SimpleNamespace(image=None, image_frame_id='frame'),
                               S_MD_SWIPE_UP=MetaDemonAssets.S_MD_SWIPE_UP)

    def test_invalid_portrait_does_not_touch_the_screen(self):
        task = self.make_task()
        GhostAssistant(task, Mock()).scan(('session', 0.0, 'not-base64!!'))
        task.screenshot.assert_not_called()

    def test_empty_portrait_is_rejected(self):
        task = self.make_task()
        GhostAssistant(task, Mock()).scan(('session', 0.0, ''))
        task.screenshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
