"""清超鬼王商店：在解厄商店里用红叶反复购买「超鬼王简易礼盒」。"""
import random
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Optional

from module.atom.click import RuleClick
from module.exception import TaskEnd
from module.logger import logger
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main
from tasks.MetaDemonShop.assets import MetaDemonShopAssets
from tasks.MetaDemonShop.page import page_meta_demon_shop

# 简易礼盒单价；红叶少于这个数就一个也买不了
BOX_PRICE = 1888
# 用「简易」区分同一页里 4888 的「超鬼王朴素礼盒」
BOX_KEYWORD = '简易'
# 图标相对商品名的位置：从名字顶边往下 ICON_TOP 开始，以名字中线为中心、宽高 ICON_SIZE 的区域。
# 点图标比点名字稳；区域比图标本身（约 75x75）小一圈，随机落点不会点到卡片外
ICON_TOP = 38
ICON_SIZE = (56, 54)
# 名字再往下就可能只露出半张卡片，先继续滑
ITEM_NAME_MAX_Y = 470
# 向上找一遍、滑回顶部、再向上找一遍
SWIPES_PER_PASS = 12

AMOUNT_UNITS = {'': 1, '万': 10000, '亿': 100000000}
AMOUNT = re.compile(r'(\d+(?:\.\d+)?)([万亿]?)')


def parse_amount(text) -> Optional[int]:
    """解析红叶数量：一万以下是 "1887"，一万以上是 "886万"、"1.68万"。认不准返回 None。"""
    text = re.sub(r'[\s,，]', '', str(text or ''))
    # 「万」偶尔会被认成形近字
    text = text.replace('方', '万').replace('力', '万').replace('萬', '万')
    match = AMOUNT.fullmatch(text)
    if not match:
        return None
    number, unit = match.groups()
    if '.' in number and not unit:
        return None
    return int(Decimal(number) * AMOUNT_UNITS[unit])


class ScriptTask(GameUi, MetaDemonShopAssets):
    # 关闭购买弹窗时可点的空白处：左侧 5 块、右侧 2 块。
    # 左侧避开返回键、名牌、「已获得皮肤」和大购买按钮；右侧避开商品卡片、活动横幅和页签——
    # 弹窗万一已经关了，点到这些上面会打开别的商品或切走页面
    buy_blanks = (MetaDemonShopAssets.C_BUY_BLANK_1, MetaDemonShopAssets.C_BUY_BLANK_2,
                  MetaDemonShopAssets.C_BUY_BLANK_3, MetaDemonShopAssets.C_BUY_BLANK_4,
                  MetaDemonShopAssets.C_BUY_BLANK_5, MetaDemonShopAssets.C_BUY_BLANK_6,
                  MetaDemonShopAssets.C_BUY_BLANK_7)

    def run(self):
        conf = self.config.meta_demon_shop.meta_demon_shop_config
        self.goto_page(page_meta_demon_shop)

        currency = self.read_currency()
        if currency is None or currency < BOX_PRICE:
            logger.info(f'红叶数量 {currency}，不足 {BOX_PRICE}，本轮不购买')
            self.finish(bought=0, call_realm_raid=False)

        bought = self.buy_boxes(conf.buy_times)
        logger.info(f'本轮购买 {bought}/{conf.buy_times} 次')
        self.finish(bought=bought, call_realm_raid=conf.call_realm_raid)

    def finish(self, bought: int, call_realm_raid: bool):
        self.close_buy_dialog()
        self.goto_page(page_main)
        if bought and call_realm_raid:
            if not self.config.realm_raid.scheduler.enable:
                logger.warning('个人突破任务未启用，设置了运行时间也不会执行')
            self.set_next_run(task='RealmRaid', server=False, target=datetime.now())
        # 买到了就按成功间隔很快再来一轮；没买到说明红叶用完或界面异常，按失败间隔
        self.set_next_run(task='MetaDemonShop', success=bool(bought), finish=True)
        raise TaskEnd('MetaDemonShop')

    def read_currency(self) -> Optional[int]:
        """读顶栏红叶数量。

        单行识别偶尔会漏掉一位数字（886万 读成 86万）且置信度照样很高，
        所以同一帧再用检测加识别读一遍，两个结果一致才采信。
        """
        readings = []
        for _ in range(5):
            self.screenshot()
            image = self.device.image
            single = parse_amount(self.O_SHOP_CURRENCY.ocr(image))
            boxes = self.O_SHOP_CURRENCY.detect_and_ocr(image, logDisplay=False)
            detected = parse_amount(''.join(box.ocr_text for box in boxes))
            if single is not None and single == detected:
                return single
            readings += [value for value in (single, detected) if value is not None]
            time.sleep(0.3)
        if not readings:
            logger.warning('红叶数量识别失败')
            return None
        # 一直对不上时取大的：漏字只会把数字读小
        logger.warning(f'红叶数量两种识别结果不一致 {readings}，取最大值')
        return max(readings)

    def buy_boxes(self, times: int) -> int:
        bought = 0
        while bought < times:
            currency = self.read_currency()
            if currency is not None and currency < BOX_PRICE:
                logger.info(f'红叶剩余 {currency}，不够再买')
                break
            if not self.open_buy_dialog():
                logger.warning('没能打开超鬼王简易礼盒的购买弹窗')
                break
            if not self.confirm_purchase(currency):
                logger.warning('购买没有成功，停止本轮')
                break
            bought += 1
            logger.info(f'已购买 {bought}/{times} 次')
        return bought

    def find_box(self) -> Optional[RuleClick]:
        """在当前帧的商品列表里找简易礼盒，返回图标的点击区域（落点在区域内随机）。"""
        roi_x, roi_y = self.O_SHOP_ITEMS.roi[0], self.O_SHOP_ITEMS.roi[1]
        for result in self.O_SHOP_ITEMS.detect_and_ocr(self.device.image, logDisplay=False):
            if BOX_KEYWORD not in result.ocr_text:
                continue
            box = result.box
            name_y = int(box[0, 1]) + roi_y
            if name_y > ITEM_NAME_MAX_Y:
                return None
            center_x = int((box[0, 0] + box[1, 0]) / 2) + roi_x
            width, height = ICON_SIZE
            roi = (center_x - width // 2, name_y + ICON_TOP, width, height)
            return RuleClick(roi_front=roi, roi_back=roi, name='shop_box')
        return None

    def open_buy_dialog(self) -> bool:
        """找到简易礼盒并点开购买弹窗；弹窗标题对不上就关掉重找。"""
        swipes = 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.screenshot()
            if self.appear(self.I_BUY_MAX):
                title = self.O_BUY_TITLE.ocr(self.device.image)
                if BOX_KEYWORD in title:
                    return True
                logger.warning(f'购买弹窗是「{title}」，不是简易礼盒，关闭重找')
                self.close_buy_dialog()
                continue
            if self.ui_reward_appear_click():
                continue
            if not self.appear(self.I_SHOP_CHECK):
                time.sleep(0.3)
                continue
            target = self.find_box()
            if target is not None:
                self.click(target)
                time.sleep(1)
                continue
            if swipes >= SWIPES_PER_PASS * 3:
                return False
            back_to_top = SWIPES_PER_PASS <= swipes < SWIPES_PER_PASS * 2
            self.swipe(self.S_SHOP_SWIPE_DOWN if back_to_top else self.S_SHOP_SWIPE_UP)
            swipes += 1
            time.sleep(1)
        return False

    def read_total_price(self) -> Optional[int]:
        """读价格按钮上的总价。数量框里的单个数字容易认错，总价位数多，更可靠。"""
        boxes = self.O_BUY_PRICE.detect_and_ocr(self.device.image, logDisplay=False)
        return parse_amount(''.join(box.ocr_text for box in boxes))

    def select_max(self, affordable: Optional[int]) -> None:
        """点「拉满」。买得起两个以上时总价应该高于单价，没变就再点。"""
        for _ in range(3):
            self.click(self.I_BUY_MAX)
            time.sleep(0.6)
            self.screenshot()
            total = self.read_total_price()
            if (total is not None and total > BOX_PRICE) or (affordable is not None and affordable < 2):
                logger.info(f'数量已拉满，总价 {total}')
                return
        logger.warning('点了拉满但总价没有变化，按当前数量购买')

    def confirm_purchase(self, before: Optional[int]) -> bool:
        """弹窗已打开：数量拉满后购买。出现获得奖励或弹窗关闭算成功。"""
        self.select_max(None if before is None else before // BOX_PRICE)
        if not self.appear(self.I_BUY_CONFIRM):
            return False
        # 红叶图标只用来确认按钮在；落点在整个价格按钮内随机
        self.click(self.C_BUY_PRICE)

        rewarded = False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.3)
            self.screenshot()
            if self.ui_reward_appear_click():
                rewarded = True
                deadline = time.monotonic() + 5
                continue
            if rewarded:
                return True
            if self.click_ui_confirm(interval=1):
                continue
            if not self.appear(self.I_BUY_MAX) and self.appear(self.I_SHOP_CHECK):
                return True
        if rewarded:
            return True
        # 弹窗一直没关也没有奖励提示：只有红叶确实少了才算买到
        after = self.read_currency()
        return before is not None and after is not None and after < before

    def close_buy_dialog(self) -> None:
        for _ in range(5):
            self.screenshot()
            if not self.appear(self.I_BUY_MAX):
                return
            # 每次随机挑一块空白区域，再在区域内随机取点
            self.click(random.choice(self.buy_blanks))
            time.sleep(1)
        logger.warning('购买弹窗没有关掉')
