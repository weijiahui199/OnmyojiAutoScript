from enum import Enum

from pydantic import Field

from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler


class BoxType(str, Enum):
    SIMPLE = '超鬼王简易礼盒（1888）'
    PLAIN = '超鬼王朴素礼盒（4888）'
    FINE = '超鬼王精致礼盒（8888）'

    @property
    def keyword(self) -> str:
        """三种礼盒名字里互不相同的两个字，用来在商品列表和购买弹窗里认礼盒。"""
        return {BoxType.SIMPLE: '简易', BoxType.PLAIN: '朴素', BoxType.FINE: '精致'}[self]

    @property
    def price(self) -> int:
        return {BoxType.SIMPLE: 1888, BoxType.PLAIN: 4888, BoxType.FINE: 8888}[self]

    @property
    def label(self) -> str:
        return f'{self.keyword}礼盒'


def shop_scheduler() -> Scheduler:
    """买完一轮就去打个人突破，两分钟后再回来买下一轮。"""
    return Scheduler(success_interval=TimeDelta(minutes=2))


class MetaDemonShopConfig(ConfigBase):
    box: BoxType = Field(default=BoxType.FINE, title='购买的礼盒',
        description='用红叶购买哪一种礼盒，括号里是单价。红叶不够买一个时本轮不购买。')
    buy_times: int = Field(default=8, ge=1, le=200, title='每轮购买次数',
        description='每次购买都会先把数量拉满（最多 50 个）再点购买；红叶不够买一个礼盒时提前结束。')
    call_realm_raid: bool = Field(default=True, title='买完后执行个人突破',
        description='本轮买到礼盒后回到庭院，并把个人突破的下次运行时间设为现在。个人突破任务本身需要已启用。')


class MetaDemonShop(ConfigBase):
    scheduler: Scheduler = Field(default_factory=shop_scheduler)
    meta_demon_shop_config: MetaDemonShopConfig = Field(default_factory=MetaDemonShopConfig)
