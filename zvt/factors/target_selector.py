import operator
from enum import Enum
from itertools import accumulate
from typing import List

import pandas as pd
from pandas import DataFrame

from zvdata import IntervalLevel
from zvt.drawer.drawer import Drawer
from zvt.factors.factor import FilterFactor, ScoreFactor, Factor
from zvdata.normal_data import NormalData
from zvdata.utils.pd_utils import index_df, df_is_not_null
from zvdata.utils.time_utils import to_pd_timestamp
from zvt.api.quote import get_securities_in_blocks


class TargetType(Enum):
    # open_long 代表开多，并应该平掉相应标的的空单
    open_long = 'open_long'
    # open_short 代表开空，并应该平掉相应标的的多单
    open_short = 'open_short'
    # 其他情况就是保持当前的持仓


class TargetSelector(object):
    def __init__(self,
                 entity_ids=None,
                 entity_type='stock',
                 exchanges=['sh', 'sz'],
                 codes=None,
                 the_timestamp=None,
                 start_timestamp=None,
                 end_timestamp=None,
                 long_threshold=0.8,
                 short_threshold=0.2,
                 level=IntervalLevel.LEVEL_1DAY,
                 provider='eastmoney',
                 block_selector=None) -> None:
        self.entity_ids = entity_ids
        self.entity_type = entity_type
        self.exchanges = exchanges
        self.codes = codes
        self.provider = provider
        self.block_selector: TargetSelector = block_selector

        if the_timestamp:
            self.the_timestamp = to_pd_timestamp(the_timestamp)
            self.start_timestamp = self.the_timestamp
            self.end_timestamp = self.the_timestamp
        elif start_timestamp and end_timestamp:
            self.start_timestamp = to_pd_timestamp(start_timestamp)
            self.end_timestamp = to_pd_timestamp(end_timestamp)
        else:
            assert False

        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        self.level = level

        self.filter_factors: List[FilterFactor] = []
        self.score_factors: List[ScoreFactor] = []
        self.filter_result = None
        self.score_result = None

        self.open_long_df: DataFrame = None
        self.open_short_df: DataFrame = None

        self.init_factors(entity_ids=entity_ids, entity_type=entity_type, exchanges=exchanges, codes=codes,
                          the_timestamp=the_timestamp, start_timestamp=start_timestamp, end_timestamp=end_timestamp,
                          level=self.level)

    def init_factors(self, entity_ids, entity_type, exchanges, codes, the_timestamp, start_timestamp, end_timestamp,
                     level):
        pass

    def add_filter_factor(self, factor: FilterFactor):
        self.check_factor(factor)
        self.filter_factors.append(factor)
        return self

    def add_score_factor(self, factor: ScoreFactor):
        self.check_factor(factor)
        self.score_factors.append(factor)
        return self

    def check_factor(self, factor: Factor):
        assert factor.level == self.level

    def move_on(self, to_timestamp=None, kdata_use_begin_time=False, timeout=20):
        if self.block_selector:
            self.block_selector.move_on(to_timestamp=to_timestamp, kdata_use_begin_time=kdata_use_begin_time,
                                        timeout=timeout)

        if self.score_factors:
            for factor in self.score_factors:
                factor.move_on(to_timestamp, timeout=timeout)
        if self.filter_factors:
            for factor in self.filter_factors:
                factor.move_on(to_timestamp, timeout=timeout)

        self.run()

    def run(self):
        """

        """
        if self.filter_factors:
            musts = []
            for factor in self.filter_factors:
                df = factor.get_result_df()

                if not df_is_not_null(df):
                    raise Exception('no data for factor:{},{}'.format(factor.factor_name, factor))

                if len(df.columns) > 1:
                    s = df.agg("and", axis="columns")
                    s.name = 'score'
                    musts.append(s.to_frame(name='score'))
                else:
                    df.columns = ['score']
                    musts.append(df)

            self.filter_result = list(accumulate(musts, func=operator.__and__))[-1]

        if self.score_factors:
            scores = []
            for factor in self.score_factors:
                df = factor.get_result_df()
                if not df_is_not_null(df):
                    raise Exception('no data for factor:{],{}'.format(factor.factor_name, factor))

                if len(df.columns) > 1:
                    s = df.agg("mean", axis="columns")
                    s.name = 'score'
                    scores.append(s.to_frame(name='score'))
                else:
                    df.columns = ['score']
                    scores.append(df)
            self.score_result = list(accumulate(scores, func=operator.__add__))[-1]

        self.generate_targets()

    def get_targets(self, timestamp, target_type: TargetType = TargetType.open_long) -> pd.DataFrame:
        if target_type == TargetType.open_long:
            df = self.open_long_df
        if target_type == TargetType.open_short:
            df = self.open_short_df

        if df_is_not_null(df):
            if timestamp in df.index:
                target_df = df.loc[[to_pd_timestamp(timestamp)], :]
                return target_df['entity_id'].tolist()
        return []

    def get_open_long_targets(self, timestamp):
        return self.get_targets(timestamp=timestamp, target_type=TargetType.open_long)

    def get_open_short_targets(self, timestamp):
        return self.get_targets(timestamp=timestamp, target_type=TargetType.open_short)

    def in_block(self, df, target_type: TargetType = TargetType.open_long):
        se = pd.Series(index=df.index)
        for index, row in df.iterrows():
            blocks = self.block_selector.get_targets(index[1], target_type=target_type)

            se[index] = False
            if blocks:
                securities = get_securities_in_blocks(provider=self.block_selector.provider, ids=blocks)
                if index[0] in securities:
                    se[index] = True

        return se

    # overwrite it to generate targets
    def generate_targets(self):
        if df_is_not_null(self.filter_result) and df_is_not_null(self.score_result):
            # for long
            result1 = self.filter_result[self.filter_result.score]
            result2 = self.score_result[self.score_result.score >= self.long_threshold]
            long_result = result2.loc[result1.index, :]
            # for short
            result1 = self.filter_result[~self.filter_result.score]
            result2 = self.score_result[self.score_result.score <= self.short_threshold]
            short_result = result2.loc[result1.index, :]
        elif df_is_not_null(self.score_result):
            long_result = self.score_result[self.score_result.score >= self.long_threshold]
            short_result = self.score_result[self.score_result.score <= self.short_threshold]
        else:
            long_result = self.filter_result[self.filter_result.score]
            short_result = self.filter_result[~self.filter_result.score]

        # filter in blocks
        if self.block_selector:
            if df_is_not_null(self.block_selector.open_long_df):
                long_result = long_result[lambda df: self.in_block(long_result, target_type=TargetType.open_long)]

            if df_is_not_null(self.block_selector.open_short_df):
                short_result = short_result[lambda df: self.in_block(short_result, target_type=TargetType.open_short)]

        self.open_long_df = self.normalize_result_df(long_result)
        self.open_short_df = self.normalize_result_df(short_result)

    def get_result_df(self):
        return self.open_long_df

    def normalize_result_df(self, df):
        if df_is_not_null(df):
            df = df.reset_index()
            df = index_df(df)
            df = df.sort_values(by=['score', 'entity_id'])
        return df

    def draw(self,
             render='html',
             file_name=None,
             width=None,
             height=None,
             title=None,
             keep_ui_state=True,
             annotation_df=None,
             target_type: TargetType = TargetType.open_long):

        if target_type == TargetType.open_long:
            df = self.open_long_df.copy()
        elif target_type == TargetType.open_short:
            df = self.open_short_df.copy()

        df['target_type'] = target_type.value

        if df_is_not_null(df):
            drawer = Drawer(
                NormalData(df=df, annotation_df=annotation_df, index_field='timestamp', is_timeseries=True))

            drawer.draw_table(render=render, file_name=file_name, width=width, height=height, title=title,
                              keep_ui_state=keep_ui_state)
