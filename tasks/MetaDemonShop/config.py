from pydantic import Field

from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler


def shop_scheduler() -> Scheduler:
    """买完一轮就去打个人突破，两分钟后再回来买下一轮。"""
    return Scheduler(success_interval=TimeDelta(minutes=2))


class MetaDemonShopConfig(ConfigBase):
    buy_times: int = Field(default=20, ge=1, le=200, title='每轮购买次数',
        description='每次购买都会先把数量拉满（最多 50 个）再点购买；红叶不够买一个礼盒时提前结束。')
    call_realm_raid: bool = Field(default=True, title='买完后执行个人突破',
        description='本轮买到礼盒后回到庭院，并把个人突破的下次运行时间设为现在。个人突破任务本身需要已启用。')


class MetaDemonShop(ConfigBase):
    scheduler: Scheduler = Field(default_factory=shop_scheduler)
    meta_demon_shop_config: MetaDemonShopConfig = Field(default_factory=MetaDemonShopConfig)
