"""Share bosses chosen by the offering side and challenge bosses shared by friends."""
from base64 import b64decode, b64encode
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import cv2
import numpy as np

from module.atom.click import RuleClick
from module.atom.swipe import RuleSwipe
from module.cooperation import Cooperation
from module.exception import RequestHumanTakeover, ScriptError, TaskEnd
from module.image.rpc import get_image_client
from module.logger import logger
from tasks.MetaDemon.config import AssistanceRole, BossType, MetaDemonMode, OfferingMode
import tasks.MetaDemon.page as pages


class AssistanceLink:
    CHANNEL = 'MetaDemonAssistance'

    def __init__(self, task):
        self.task = task
        settings = task.conf.assistance_config
        self.assist = settings.role == AssistanceRole.ASSIST
        self.peer = settings.cooperation_script
        self.runtime = getattr(task.config, 'cooperation_runtime', None) or Cooperation(task.config.config_name)
        self.session = uuid4().hex
        # 协助端可以同时服务多个上供端：逐个处理它们分享的鬼王。
        self.current_peer = None
        self.settled_sessions = set()
        other_role = AssistanceRole.ASSIST if settings.role == AssistanceRole.OFFERING else AssistanceRole.OFFERING
        if self.peer:
            self.peers = [self.peer]
        else:
            peers = []
            for path in sorted((self.runtime.root / 'config').glob('*.json')):
                if path.stem == self.runtime.name:
                    continue
                other = self.runtime.read(path.stem).get('meta_demon', {})
                other_settings = other.get('assistance_config', {})
                if (other.get('scheduler', {}).get('enable')
                        and other_settings.get('role') == other_role
                        and other_settings.get('cooperation_script') == self.runtime.name):
                    peers.append(path.stem)
            if len(peers) > 1 and not self.assist:
                raise ScriptError('有多个鬼王协助脚本指向当前实例，请明确填写协同脚本')
            self.peers = peers
            self.peer = next(iter(peers), '')
        for peer in self.peers:
            other = self.runtime.read(peer).get('meta_demon', {})
            other_settings = other.get('assistance_config', {})
            if (peer == self.runtime.name or other_settings.get('role') != other_role
                    or MetaDemonMode.BOSS_ASSIST not in other.get('meta_demon_config', {}).get('mode', [])
                    or other_settings.get('cooperation_script', '') not in ('', self.runtime.name)):
                raise ScriptError('鬼王协同脚本应为匹配的上供和协助实例')
        if len(self.peers) > 1:
            logger.info(f'鬼王协助关联多个上供端：{self.peers}')

    def publish(self, portrait='', star=None):
        if self.peer:
            if portrait:
                self.session = uuid4().hex
            remaining = (self.task.limit_time - (datetime.now() - self.task.start_time)).total_seconds()
            payload = {
                'portrait': portrait,
                'expires': time.time() + max(0, remaining),
            }
            if star is not None:
                payload['star'] = int(star)
            self.runtime.publish_battle_state(self.CHANNEL, self.peer, self.session, payload)

    def target_star(self, target):
        """读取上供端随鬼王截图一起同步的星级。"""
        state = self.runtime.peer_battle_state(self.CHANNEL, target[3])
        if not state or state['session'] != target[0]:
            return None
        try:
            return BossType(int(state['payload'].get('star')))
        except (TypeError, ValueError):
            return None

    def _peer_target(self, peer):
        state = self.runtime.peer_battle_state(self.CHANNEL, peer)
        if not state or state['session'] in self.settled_sessions:
            return None
        payload = state['payload']
        portrait = payload.get('portrait')
        if portrait and payload.get('expires', 0) > time.time():
            return state['session'], state['updated'], portrait, peer
        return None

    def target(self):
        """当前要协助的鬼王；正在处理的上供端优先，否则取最早分享的那只。"""
        if self.current_peer:
            current = self._peer_target(self.current_peer)
            if current:
                return current
            self.current_peer = None
        candidates = [item for item in map(self._peer_target, self.peers) if item]
        if not candidates:
            return None
        chosen = min(candidates, key=lambda item: item[1])
        self.current_peer = chosen[3]
        if len(self.peers) > 1:
            logger.info(f'协助切换到上供端：{chosen[3]}')
        return chosen

    def confirm(self, target, *, settled=False):
        self.runtime.publish_battle_state(self.CHANNEL, target[3], target[0], {
            'matched': True, 'settled': settled,
        })
        if settled:
            self.settled_sessions.add(target[0])

    def confirmed(self):
        state = self.runtime.peer_battle_state(self.CHANNEL, self.peer)
        return bool(state and state['session'] == self.session and state['payload'].get('matched'))

    def peer_settled(self):
        state = self.runtime.peer_battle_state(self.CHANNEL, self.peer)
        return bool(state and state['session'] == self.session and state['payload'].get('settled'))

    REST_TABLE = 'meta_demon_assist_rest'

    @contextmanager
    def _rest_db(self):
        with self.runtime.connect() as conn:
            conn.execute(f'CREATE TABLE IF NOT EXISTS {self.REST_TABLE} (name TEXT PRIMARY KEY, until TEXT)')
            yield conn

    def mark_rest(self, until):
        """协助端疲劳度满、离开超鬼王休息：记下回来的时间，上供端到时再上供。"""
        with self._rest_db() as conn:
            conn.execute(f'INSERT OR REPLACE INTO {self.REST_TABLE} VALUES (?, ?)',
                         (self.runtime.name, until.strftime('%Y-%m-%d %H:%M:%S')))
        logger.info(f'协助端疲劳度已满，休息到 {until:%H:%M:%S}，上供端届时再继续')

    def clear_rest(self):
        with self._rest_db() as conn:
            conn.execute(f'DELETE FROM {self.REST_TABLE} WHERE name=?', (self.runtime.name,))

    def peer_rest_until(self):
        """上供端：协助端还在因疲劳休息时返回它回来的时间，否则返回 None。"""
        if not self.peer:
            return None
        with self._rest_db() as conn:
            row = conn.execute(f'SELECT until FROM {self.REST_TABLE} WHERE name=?', (self.peer,)).fetchone()
        if not row:
            return None
        until = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
        return until if until > datetime.now() else None

    def peer_in_task(self):
        return any(self._peer_cooperating(peer) for peer in self.peers)

    def _peer_cooperating(self, peer):
        """对端正在跑超鬼王，且不是「不协同」的上供号（那种号不会和本号配合）。"""
        if self.runtime.runner_task(peer) != 'MetaDemon':
            return False
        settings = self.runtime.read(peer).get('meta_demon', {}).get('assistance_config', {})
        return (settings.get('role') != AssistanceRole.OFFERING
                or settings.get('offering_mode', OfferingMode.COOPERATIVE) == OfferingMode.COOPERATIVE)

    def wait_for_peer(self):
        if len(self.peers) > 1:
            return self._wait_for_any_peer()
        task = 'MetaDemon'
        if not self.runtime.prepare(self.task.config, task, self.peer, defer=False):
            return False
        deadline = time.monotonic() + min(30, self.task.limit_time.total_seconds())
        while time.monotonic() < deadline:
            running = self.runtime.runner_task(self.peer)
            if running == task:
                return True
            if running != '':
                break
            time.sleep(0.5)
        self.runtime.skip_without_wake(task, self.peer, '对端未进入超鬼王协同任务')
        logger.info(f'超鬼王协同跳过：{self.peer} 未进入同一任务')
        return False

    def _wait_for_any_peer(self):
        """协助端关联多个上供端：唤醒在线的上供端，任意一个进入超鬼王即可开始。"""
        task = 'MetaDemon'
        for peer in self.peers:
            # 没有心跳的实例不等待，避免逐个等 30 秒。
            if self.runtime.runner_task(peer) is None:
                logger.info(f'超鬼王协同：{peer} 未运行，跳过')
                continue
            try:
                self.runtime.prepare(self.task.config, task, peer, defer=False)
            except ScriptError as exc:
                logger.info(f'超鬼王协同：{peer} 配置不匹配，跳过：{exc}')
        # prepare 在对端忙碌时会留下本实例的等待记录，会让对端随后加入时误判为忙碌。
        with self.runtime.connect() as db:
            db.execute('DELETE FROM waiting WHERE name=? AND task=?', (self.runtime.name, task))
        deadline = time.monotonic() + min(30, self.task.limit_time.total_seconds())
        while time.monotonic() < deadline:
            if self.peer_in_task():
                return True
            time.sleep(0.5)
        for peer in self.peers:
            self.runtime.skip_without_wake(task, peer, '对端未进入超鬼王协同任务')
        logger.info(f'超鬼王协同跳过：{self.peers} 均未进入同一任务')
        return False


class GhostOffering:
    INVITE_RETRY_SECONDS = 20
    # 好友行点击被吞（面板滑入动画）时的补点间隔
    CLICK_RETRY_SECONDS = 1.2

    def __init__(self, task, link=None, stars=None):
        self.task = task
        self.link = link
        self.settings = task.conf.assistance_config
        self.stars = tuple(stars) if stars else self.settings.offering_stars
        self.stars_text = '/'.join(f'{int(star)}星' for star in self.stars)
        self.shared_generation = None
        self.waiting = False
        self.settled = False
        self.empty_frames = 0
        self.unknown_reported = False

    def within_limit(self):
        t = self.task
        return (datetime.now() - t.start_time < t.limit_time and
                t.total_count < t.limit_count and not t.State.done)

    @staticmethod
    def normalize_name(name):
        return re.sub(r'\s+', '', str(name))

    def friend_rows(self):
        t = self.task
        x, y, _, _ = t.O_MD_FRIENDS.roi
        rows = {}
        for result in t.O_MD_FRIENDS.detect_and_ocr(t.device.image):
            name = self.normalize_name(result.ocr_text)
            box = result.box
            left, top = int(min(box[:, 0])) + x, int(min(box[:, 1])) + y
            width, height = max(1, int(max(box[:, 0]) - min(box[:, 0]))), max(1, int(max(box[:, 1]) - min(box[:, 1])))
            rows.setdefault(name, []).append((left, top, width, height))
        return rows

    def selected_count(self):
        t = self.task
        # 与 self 分支一致：用 NMS 去重后的结果计数，避免同一勾选被重复统计成多人。
        return len(t.I_MD_FRIEND_SELECTED.match_all_any(t.device.image, frame_id=t.device.image_frame_id))

    def list_to_top(self):
        t = self.task
        swipe = t.S_MD_SWIPE_FRIEND_UP
        reverse = RuleSwipe(roi_front=swipe.roi_back, roi_back=swipe.roi_front,
                            mode='default', name='metademon_friend_list_to_top')
        previous = None
        stable = 0
        for _ in range(20):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            signature = tuple(self.friend_rows())
            stable = stable + 1 if signature == previous else 0
            if stable >= 2:
                return True
            previous = signature
            t.swipe(reverse, interval=1.2)
        return False

    def open_share_panel(self):
        t = self.task
        deadline = time.monotonic() + 12
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if t.appear(t.I_MD_INVITE_FRIENDS):
                return True
            t.appear_then_click(t.I_MD_INVITE, interval=1.5)
        return False

    def select_friend(self, name, area):
        t = self.task
        before = self.selected_count()
        rule = RuleClick(roi_front=area, roi_back=area, name='metademon_offering_friend')
        deadline = time.monotonic() + 6
        next_click = 0.0
        attempts = 0
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            selected = self.selected_count()
            if selected == before + 1:
                logger.info(f'鬼王上供已选好友: {name}')
                return True
            # 面板滑入动画或点击穿透会吞掉第一次点击，没选中就重试；
            # 已有选中时不再补点，避免把刚选上的好友又点掉。
            if selected == before and attempts < 3 and time.monotonic() >= next_click:
                attempts += 1
                t.click(rule)
                next_click = time.monotonic() + self.CLICK_RETRY_SECONDS
        logger.warning(f'好友 {name} 点击后未确认选中（点击 {attempts} 次），停止本次分享')
        return False

    def select_friends(self):
        t = self.task
        wanted = {self.normalize_name(name): name for name in self.settings.friends}
        if not wanted:
            return False
        # 分享面板刚打开时列表还在滑入，只能识别到一两个名字；等两次识别结果一致再判断，
        # 否则会误以为好友不在当前页而去滑动列表，短滑动还可能被当成点击误选好友。
        rows, previous = {}, None
        for _ in range(10):
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS) or self.selected_count():
                break
            rows = self.friend_rows()
            if rows and previous is not None and rows.keys() == previous.keys():
                break
            previous = rows
            time.sleep(0.3)
        if not t.appear(t.I_MD_INVITE_FRIENDS) or self.selected_count():
            logger.warning('分享列表存在已有选择或已关闭，请清空好友选择后重试')
            return False
        # All recipients are already visible. Avoid moving away from their rows.
        if wanted.keys() <= rows.keys():
            for name in wanted:
                rows = self.friend_rows()
                if len(rows.get(name, ())) != 1 or not self.select_friend(name, rows[name][0]):
                    return False
            return True
        if not self.list_to_top():
            return False
        # Inspect the whole list before selecting, so stale selections cannot
        # silently include an unconfigured recipient on an off-screen row.
        previous = None
        stable = 0
        available = set()
        for _ in range(30):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS) or self.selected_count():
                logger.warning('分享列表存在已有选择或已关闭，请清空好友选择后重试')
                return False
            rows = self.friend_rows()
            if any(len(rows.get(name, [])) > 1 for name in wanted):
                logger.warning('好友名字有多个相同识别结果，无法唯一选择')
                return False
            available.update(rows)
            signature = tuple(rows)
            stable = stable + 1 if signature == previous else 0
            if stable >= 2:
                break
            previous = signature
            t.swipe(t.S_MD_SWIPE_FRIEND_UP, interval=1.2)
        else:
            logger.warning('好友列表扫描超过限制，停止分享')
            return False
        missing = wanted.keys() - available
        if missing:
            logger.warning('未找到指定好友: ' + '; '.join(wanted[name] for name in missing))
            return False
        if not self.list_to_top():
            return False
        remaining = dict(wanted)
        for _ in range(30):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            rows = self.friend_rows()
            for name in tuple(remaining):
                # Selection can reorder rows; resolve each subsequent name on
                # the latest screenshot instead of reusing an earlier box.
                rows = self.friend_rows()
                if name not in rows:
                    continue
                if len(rows[name]) != 1 or not self.select_friend(remaining[name], rows[name][0]):
                    return False
                del remaining[name]
            if not remaining:
                return True
            t.swipe(t.S_MD_SWIPE_FRIEND_UP, interval=1.2)
        return False

    def share_with_friends(self):
        t = self.task
        if not self.open_share_panel() or not self.select_friends():
            return False
        t.screenshot()
        if self.selected_count() != len(self.settings.friends):
            logger.warning('分享前好友选中数量不符，停止本次上供')
            return False
        if not t.appear_then_click(t.I_MD_INVITE_FRIENDS, interval=self.INVITE_RETRY_SECONDS):
            return False
        deadline = time.monotonic() + self.INVITE_RETRY_SECONDS
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if t.appear(t.I_MD_INVITE_FRIENDS):
                continue
            if any(t.appear(rule) for rule in (
                    t.I_MD_FIRE, t.I_MD_SETTLING, t.I_MD_SUMMON_BOSS, t.I_UI_REWARD)):
                logger.info('已按好友名字分享鬼王，等待好友击杀')
                return True
        logger.warning('未确认好友分享完成，停止本次上供')
        return False

    def share(self):
        t = self.task
        if t.cur_boss_type not in self.stars:
            logger.warning(f'仅允许分享本次已确认召唤的{self.stars_text}鬼王')
            return False
        if self.link is None:
            return self.share_with_friends()
        portrait_sent = False
        select_recipients = True
        while self.within_limit():
            if not self.link.peer_in_task():
                logger.info('协助端已离开超鬼王，停止重试邀请')
                return False
            if portrait_sent and self.link.confirmed():
                logger.info('协助端已识别分享鬼王，等待好友击杀')
                return True
            if select_recipients and (not self.open_share_panel() or not self.select_friends()):
                return False
            t.screenshot()
            if portrait_sent and self.link.confirmed():
                t.appear_then_click(t.I_MD_CLOSE_POPUP, interval=0.8)
                logger.info('协助端已识别分享鬼王，等待好友击杀')
                return True
            if not select_recipients and self.selected_count() != len(self.settings.friends):
                logger.warning('邀请重试前好友选中状态已变化，停止本次上供')
                return False
            if not t.appear_then_click(t.I_MD_INVITE_FRIENDS, interval=self.INVITE_RETRY_SECONDS):
                return False
            deadline = time.monotonic() + self.INVITE_RETRY_SECONDS
            select_recipients = False
            while self.within_limit() and time.monotonic() < deadline:
                if portrait_sent and self.link.confirmed():
                    logger.info('协助端已识别分享鬼王，等待好友击杀')
                    return True
                t.screenshot()
                if t.appear(t.I_MD_INVITE_FRIENDS):
                    continue
                select_recipients = True
                if t.appear(t.I_MD_SUMMON_BOSS) and not portrait_sent:
                    return False
                if not portrait_sent and t.appear(t.I_MD_FIRE):
                    portrait_sent = self.send_portrait()
                    if not portrait_sent:
                        return False
            if self.within_limit():
                logger.info(f'邀请已等待 {self.INVITE_RETRY_SECONDS} 秒，未收到协助端确认，准备重试')
        logger.warning('运行时间内未收到协助端确认，停止本次上供')
        return False

    def send_portrait(self):
        t = self.task
        x, y, w, h = t.I_SHARE_DEMON.roi_back
        portrait = t.device.image[y:y + h, x:x + w]
        if portrait.shape[:2] != (h, w):
            logger.warning('上供鬼王截图区域无效')
            return False
        encoded, png = cv2.imencode('.png', cv2.cvtColor(portrait, cv2.COLOR_RGB2BGR))
        if not encoded:
            logger.warning('上供鬼王截图编码失败')
            return False
        self.link.publish(b64encode(png).decode('ascii'), star=t.cur_boss_type)
        logger.info('已同步上供鬼王截图，等待协助端确认')
        return True

    def tick(self):
        """Return False only when this run should stop; never enter a battle."""
        t = self.task
        if t.appear(t.I_UI_REWARD):
            t.click(t.C_UI_REWARD, interval=0.8)
            return True
        settling = t.appear(t.I_MD_SETTLING)
        if (self.shared_generation is not None and not self.settled
                and (settling or (self.link is not None and self.link.peer_settled()))):
            self.settled = True
            if self.link is not None:
                self.link.publish()
            logger.info('上供鬼王进入结算，等待召唤标志后再开始下一轮')
        if settling:
            self.empty_frames = 0
            return True
        if not self.settled and t.appear(t.I_MD_FIRE):
            self.empty_frames = 0
            if t.cur_boss_type not in self.stars:
                if not self.unknown_reported:
                    logger.warning(f'已有鬼王星级未确认或不满足上供要求，不分享、不挑战；等待其消失后仅召唤{self.stars_text}')
                    self.unknown_reported = True
            elif self.shared_generation != t._boss_generation:
                if not self.share():
                    return False
                self.shared_generation = t._boss_generation
            self.waiting = True
            return True
        if not t.appear(t.I_MD_SUMMON_BOSS):
            self.empty_frames = 0
            return True
        if self.shared_generation is not None and not self.settled and self.link is not None:
            self.empty_frames = 0
            return True
        self.empty_frames += 1
        if self.empty_frames < 2:
            return True
        if self.waiting:
            if self.shared_generation is not None:
                t.total_count += 1
                logger.info(f'上供鬼王已结束: {t.total_count}/{t.limit_count}')
            self.waiting = False
            self.settled = False
            self.shared_generation = None
            t.cur_boss_type = None
            self.unknown_reported = False
            self.remember_summon(None)
            if not self.within_limit():
                return True
        self.empty_frames = 0
        summoned = t.check_and_switch_ticket(sequence=self.stars)
        if summoned and t.cur_boss_type is not None:
            self.remember_summon(t.cur_boss_type)
        return summoned or t.State.done

    # 鬼王召唤后约 20 分钟逃跑。
    SUMMON_MEMORY_SECONDS = 20 * 60
    SUMMON_TABLE = 'meta_demon_last_summon'

    def _summon_db(self):
        runtime = self.link.runtime
        db = runtime.connect()
        return runtime.name, db

    def remember_summon(self, star):
        """记录本号刚召唤的鬼王星级；上供中途失败重跑时，可以继续分享这只鬼王而不是等它逃跑。"""
        if self.link is None or getattr(self.link, 'runtime', None) is None:
            return
        name, db = self._summon_db()
        with db as conn:
            conn.execute(f'CREATE TABLE IF NOT EXISTS {self.SUMMON_TABLE} '
                         '(name TEXT PRIMARY KEY, star INTEGER, created REAL)')
            if star is None:
                conn.execute(f'DELETE FROM {self.SUMMON_TABLE} WHERE name=?', (name,))
            else:
                conn.execute(f'INSERT OR REPLACE INTO {self.SUMMON_TABLE} VALUES (?, ?, ?)',
                             (name, int(star), time.time()))

    def restore_summon(self):
        """本次运行开始时页面上已有鬼王：若是本号 20 分钟内召唤的，恢复它的星级。"""
        t = self.task
        if t.cur_boss_type is not None or self.link is None or getattr(self.link, 'runtime', None) is None:
            return
        name, db = self._summon_db()
        with db as conn:
            conn.execute(f'CREATE TABLE IF NOT EXISTS {self.SUMMON_TABLE} '
                         '(name TEXT PRIMARY KEY, star INTEGER, created REAL)')
            row = conn.execute(f'SELECT star, created FROM {self.SUMMON_TABLE} WHERE name=?', (name,)).fetchone()
        if not row or time.time() - row[1] > self.SUMMON_MEMORY_SECONDS:
            return
        try:
            star = BossType(int(row[0]))
        except ValueError:
            return
        t.cur_boss_type = star
        t._boss_generation = getattr(t, '_boss_generation', 0) + 1
        logger.info(f'页面已有鬼王：按本号 {int(time.time() - row[1])} 秒前的召唤记录视为{int(star)}星，继续分享')


class GhostAssistant:
    def __init__(self, task, link):
        self.task = task
        self.link = link
        self.active_target = None
        self.settled_target = None
        swipe = task.S_MD_SWIPE_UP
        self.reverse = RuleSwipe(roi_front=swipe.roi_back, roi_back=swipe.roi_front,
                                 mode='default', name='metademon_assist_list_to_top')

    def within_limit(self):
        t = self.task
        t.update_global_state()
        return not t.State.done

    def match_portrait(self, template):
        t = self.task
        rule = t.I_FIGHT_SHARE_DEMON
        result = get_image_client().match_dynamic_template(
            template=template, image=t.device.image, frame_id=t.device.image_frame_id,
            roi_back=rule.roi_back, threshold=rule.threshold, name=rule.name,
        )
        return tuple(result['roi_front']) if result['matched'] else None

    def open_row(self, area):
        t = self.task
        t.click(RuleClick(roi_front=area, roi_back=area, name='metademon_assist_row'))
        time.sleep(0.6)
        deadline = time.monotonic() + 5
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if t.appear(t.I_MD_SETTLING):
                return False
            if t.appear(t.I_MD_FIRE):
                return True
        return False

    def return_to_list(self):
        t = self.task
        t.screenshot()
        if not t.match_page_once(pages.page_meta_demon_boss):
            t.appear_then_click(t.I_MD_CLOSE_POPUP, interval=0.8)
            t.goto_page(pages.page_meta_demon_boss, skip_first_screenshot=False)

    def fight(self, target):
        t = self.task
        if self.active_target != target:
            t.cur_boss_type = (self.link.target_star(target)
                               or min(t.conf.assistance_config.offering_stars))
            t._boss_generation += 1
            t._current_boss_attempts = 0
            self.active_target = target
        while self.within_limit():
            t.screenshot()
            if self.observe_settlement(target) or not t.appear(t.I_MD_FIRE) or self.link.target() != target:
                break
            if not (t.check_fatigue() and t.check_and_switch_soul()
                    and t.check_and_switch_powerful()):
                break
            if self.link.target() != target:
                break
            if not t.start_battle(within_limit=self.within_limit):
                self.observe_settlement(target)
                break
            if self.observe_settlement(target):
                break

    def observe_settlement(self, target):
        if self.settled_target == target:
            return True
        if not self.task.appear(self.task.I_MD_SETTLING):
            return False
        self.settled_target = target
        if self.link.target() == target:
            self.link.confirm(target, settled=True)
        logger.info('协助鬼王进入结算，等待上供端重新召唤')
        return True

    def scan(self, target):
        t = self.task
        try:
            data = np.frombuffer(b64decode(target[2], validate=True), dtype=np.uint8)
            portrait = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        except (ValueError, cv2.error):
            portrait = None
        if portrait is None:
            logger.warning('协助收到的鬼王截图无效')
            return
        template = cv2.cvtColor(portrait, cv2.COLOR_BGR2RGB)
        for swipe in (t.S_MD_SWIPE_UP, self.reverse):
            for _ in range(12):
                if not self.within_limit() or self.link.target() != target:
                    return
                t.screenshot()
                area = self.match_portrait(template)
                if area and self.link.target() == target:
                    logger.info(f'协助匹配到鬼王头像：{area}')
                    self.link.confirm(target)
                    opened = self.open_row(area)
                    if not self.observe_settlement(target) and opened and self.link.target() == target:
                        self.fight(target)
                    self.return_to_list()
                    return
                t.swipe(swipe)
                time.sleep(0.5)

    # 轮流上供时，上一个号交接给下一个号之间会有空档，协助端在此期间继续等待。
    HANDOFF_GRACE_SECONDS = 180

    def run(self):
        # 回到超鬼王说明已经不再休息；之后疲劳度满时记录休息时间，让上供端等待。
        self.link.clear_rest()
        self.task.on_fatigue_full = self.link.mark_rest
        # 等待对端可能已超过截图缓存期限（3 秒），必须重新截图再判断页面。
        self.task.goto_page(pages.page_meta_demon_boss, skip_first_screenshot=False)
        waiting_logged = False
        grace = self.HANDOFF_GRACE_SECONDS if len(self.link.peers) > 1 else 0
        idle_since = None
        while self.within_limit():
            if not self.link.peer_in_task():
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= grace:
                    logger.info('上供端已离开超鬼王，结束本轮协助')
                    return False
                if not waiting_logged:
                    logger.info(f'上供端暂时都不在超鬼王，等待下一个上供号（最多 {grace} 秒）')
                    waiting_logged = True
                self.task.device.stuck_record_clear()
                time.sleep(self.task.conf.assistance_config.poll_interval)
                continue
            if idle_since is not None:
                idle_since = None
                waiting_logged = False
            self.task.device.stuck_record_clear()
            target = self.link.target()
            if target and target != self.settled_target:
                waiting_logged = False
                self.scan(target)
            elif not waiting_logged:
                logger.info('协助已进入鬼王挑战页，等待上供者同步鬼王截图')
                waiting_logged = True
            if self.within_limit():
                time.sleep(self.task.conf.assistance_config.poll_interval)
        return True


def run_offering(task, link=None, stars=None):
    """link 为空表示不协同：按好友名字分享，不等协助脚本确认。"""
    # 等待协助端可能已超过截图缓存期限（3 秒），必须重新截图再判断页面。
    task.goto_page(pages.page_meta_demon_boss, skip_first_screenshot=False)
    offering = GhostOffering(task, link, stars=stars)
    offering.restore_summon()
    success = True
    while offering.within_limit():
        if link is not None and not link.peer_in_task():
            logger.info('协助端已离开超鬼王，结束本轮上供')
            return False
        # Waiting for someone else to kill a boss is intentional inactivity.
        task.device.stuck_record_clear()
        task.screenshot()
        if not offering.tick():
            success = False
            logger.warning('鬼王上供流程未完成，结束本次运行')
            break
        remaining = max(0, (task.limit_time - (datetime.now() - task.start_time)).total_seconds())
        interval = 0.5 if link is not None and offering.waiting and not offering.settled else offering.settings.poll_interval
        time.sleep(min(interval, remaining))
    return success


def run_assistance(task):
    settings = task.config.meta_demon.assistance_config
    offering = settings.role == AssistanceRole.OFFERING
    if offering and not settings.friends:
        raise RequestHumanTakeover('鬼王上供需要填写分享好友名字')
    task.before_run()
    # 填了「下一个上供脚本」：本号门票用完后唤醒它，中途失败时几分钟后重试，不按失败间隔推到第二天。
    relay = offering and bool(settings.next_offering_script)
    if not settings.requires_cooperation:
        # 不协同：按好友名字分享，不关联协助脚本
        success = run_offering(task)
        if relay:
            _finish_relay(task, settings.next_offering_script, success)
            return
        task.set_next_run(task='MetaDemon', success=success)
        task.finish_task()
        return
    try:
        link = AssistanceLink(task)
        if not link.peers and not offering:
            raise RequestHumanTakeover('鬼王协助需要关联对端脚本，以传送截图和确认分享')
        if offering:
            _defer_while_peer_rests(task, link)
        if offering and link.peer and link.runtime.runner_task(link.peer) != 'MetaDemon':
            # 协助端可能刚结束上一段协助，让它重新进入超鬼王。
            wake_script(link.peer)
        ready = bool(link.peer and link.wait_for_peer())
    except ScriptError as exc:
        if not offering:
            raise
        logger.info(f'鬼王上供协同未匹配：{exc}')
        ready = False
    if not ready:
        if relay:
            task.set_next_run(task='MetaDemon', target=datetime.now() + timedelta(minutes=OFFERING_RETRY_MINUTES))
        else:
            task.set_next_run(task='MetaDemon', success=False)
        if offering:
            logger.info('未匹配到可用的鬼王协助脚本，跳过本次上供')
            raise TaskEnd
        task.finish_task()
        return
    if settings.role == AssistanceRole.ASSIST:
        success = GhostAssistant(task, link).run()
    else:
        link.publish()
        try:
            success = run_offering(task, link)
        finally:
            link.publish()
        if not success:
            _defer_while_peer_rests(task, link, finish=True)
        if relay:
            _finish_relay(task, settings.next_offering_script, success)
            return
    task.set_next_run(task='MetaDemon', success=success)
    task.finish_task()


def _defer_while_peer_rests(task, link, finish=False):
    """协助端因疲劳休息时，本号等它回来再上供，期间不去唤醒它。"""
    until = link.peer_rest_until()
    if until is None:
        return
    logger.info(f'协助端 {link.peer} 疲劳度已满，休息到 {until:%H:%M:%S}，本号到时再上供')
    task.set_next_run(task='MetaDemon', target=until + timedelta(minutes=1))
    if finish:
        task.finish_task()
    raise TaskEnd


def _finish_relay(task, next_name, success):
    in_limit = (datetime.now() - task.start_time < task.limit_time and task.total_count < task.limit_count)
    if success and task.State.done and in_limit:
        logger.info(f'本号门票已按顺序用完，唤醒下一个上供脚本 {next_name}')
        if next_name != task.config.config_name:
            wake_script(next_name)
        task.set_next_run(task='MetaDemon', success=True)
    elif success:
        # 运行时间或次数到上限但门票还没用完：马上继续。
        logger.info('本号门票未用完，继续上供')
        task.set_next_run(task='MetaDemon', target=datetime.now())
    else:
        logger.warning(f'本号上供未完成，{OFFERING_RETRY_MINUTES} 分钟后重试')
        task.set_next_run(task='MetaDemon', target=datetime.now() + timedelta(minutes=OFFERING_RETRY_MINUTES))
    task.finish_task()


def wake_script(name: str) -> None:
    """把另一个实例的超鬼王运行时间设为现在；对方空闲时会在几秒内重新读取配置并开始。"""
    from pathlib import Path
    from module.config.config_model import ConfigModel
    # ConfigModel 找不到文件时会用默认值新建一个配置，必须先确认文件存在。
    if not (Path.cwd() / 'config' / f'{name}.json').is_file():
        logger.warning(f'找不到配置 {name}，无法设置超鬼王运行时间')
        return
    model = ConfigModel(config_name=name)
    model.meta_demon.scheduler.next_run = datetime.now().replace(microsecond=0)
    model.save()
    logger.info(f'已把 {name} 的超鬼王运行时间设为现在')


OFFERING_RETRY_MINUTES = 3
