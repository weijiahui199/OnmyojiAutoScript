"""多个上供号轮流上供。

协助端的「上供顺序」列出上供脚本。每一轮只召唤一个星级：顺序里的上供号一个一个来，
当前号把这个星级的门票用完后，把下一个号的超鬼王运行时间设为现在；最后一个号用完后，
从第一个号开始上供下一个星级。所有星级都轮完后本轮结束，等第一个号下次按自己的调度开始新一轮。
"""
import re
import time
from datetime import datetime

from module.logger import logger
from tasks.MetaDemon.config import AssistanceRole, BossType, OfferingStar

# 轮到的号超过这么久没有动静，视为上一轮已中断，重新从第一个号开始。
STALE_SECONDS = 6 * 3600


def parse_order(value: str) -> list[str]:
    return list(dict.fromkeys(name.strip() for name in re.split(r'[;；,，\n\r]+', str(value or '')) if name.strip()))


class OfferingChain:
    TABLE = 'meta_demon_offering_chain'

    def __init__(self, runtime, assist: str, order: list[str], phases: tuple[BossType, ...]):
        self.runtime = runtime
        self.name = runtime.name
        self.assist = assist
        self.order = order
        self.phases = phases
        with self.runtime.connect() as db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.TABLE} '
                       '(assist TEXT PRIMARY KEY, phase INTEGER, turn TEXT, updated REAL)')

    @classmethod
    def load(cls, task, runtime):
        """当前实例是协助端上供顺序里的上供号时返回轮流状态，否则返回 None。"""
        settings = task.conf.assistance_config
        assist = settings.cooperation_script
        if settings.role != AssistanceRole.OFFERING or not assist:
            return None
        assist_settings = runtime.read(assist).get('meta_demon', {}).get('assistance_config', {})
        order = parse_order(assist_settings.get('offering_order', ''))
        if runtime.name not in order:
            return None
        try:
            phases = OfferingStar(assist_settings.get('offering_star')).stars
        except ValueError:
            phases = settings.offering_star.stars
        return cls(runtime, assist, order, phases)

    def _row(self, db):
        return db.execute(f'SELECT phase, turn, updated FROM {self.TABLE} WHERE assist=?',
                          (self.assist,)).fetchone()

    def _write(self, db, phase, turn):
        db.execute(f'INSERT OR REPLACE INTO {self.TABLE} VALUES (?, ?, ?, ?)',
                   (self.assist, phase, turn, time.time()))

    def state(self):
        with self.runtime.connect() as db:
            row = self._row(db)
        return None if row is None else {'phase': row[0], 'turn': row[1], 'updated': row[2]}

    def my_star(self) -> BossType | None:
        """轮到本号时返回本号这一轮要上供的星级，没轮到返回 None。"""
        with self.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db)
            idle = (row is None or not row[1] or row[1] not in self.order
                    or not 0 <= row[0] < len(self.phases) or time.time() - row[2] > STALE_SECONDS)
            if idle:
                if self.name != self.order[0]:
                    return None
                self._write(db, 0, self.name)
                logger.info(f'轮流上供开始新一轮：顺序 {self.order}，星级 {[int(s) for s in self.phases]}')
                return self.phases[0]
            phase, turn, _ = row
            if turn != self.name:
                return None
            self._write(db, phase, turn)
            return self.phases[phase]

    def advance(self) -> str | None:
        """本号这一星级上供完毕，交给下一个号；返回下一个号，整轮结束返回 None。"""
        with self.runtime.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db)
            if row is None or row[1] != self.name:
                return None
            phase = row[0]
            index = self.order.index(self.name)
            for _ in range(len(self.order) * len(self.phases)):
                index += 1
                if index >= len(self.order):
                    index, phase = 0, phase + 1
                if phase >= len(self.phases):
                    self._write(db, 0, '')
                    logger.info('轮流上供：所有上供号的所有星级都已完成，本轮结束')
                    return None
                candidate = self.order[index]
                if self._usable(candidate):
                    self._write(db, phase, candidate)
                    logger.info(f'轮流上供：交给 {candidate}，上供{int(self.phases[phase])}星')
                    return candidate
                logger.warning(f'轮流上供：{candidate} 未启用超鬼王上供，跳过')
            self._write(db, 0, '')
            return None

    def _usable(self, name: str) -> bool:
        if name == self.name:
            return True
        try:
            item = self.runtime.read(name).get('meta_demon', {})
        except Exception as exc:
            logger.warning(f'轮流上供：读取 {name} 配置失败：{exc}')
            return False
        settings = item.get('assistance_config', {})
        return bool(item.get('scheduler', {}).get('enable')
                    and settings.get('role') == AssistanceRole.OFFERING
                    and settings.get('cooperation_script') == self.assist)

    def wake(self, name: str) -> None:
        """把另一个实例的超鬼王运行时间设为现在；对方空闲时会在几秒内重新读取配置并开始。"""
        from pathlib import Path
        from module.config.config_model import ConfigModel
        # ConfigModel 找不到文件时会用默认值新建一个配置，必须先确认文件存在。
        if not (Path.cwd() / 'config' / f'{name}.json').is_file():
            logger.warning(f'轮流上供：找不到配置 {name}，无法设置运行时间')
            return
        model = ConfigModel(config_name=name)
        model.meta_demon.scheduler.next_run = datetime.now().replace(microsecond=0)
        model.save()
        logger.info(f'轮流上供：已把 {name} 的超鬼王运行时间设为现在')
