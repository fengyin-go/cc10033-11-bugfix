"""事实关联维度统一校验测试。

覆盖任务场景：
- 混合关联维度（DIM_TIME + 不存在的 DIM_MISSING）在登记入口被拦截，不再保存
- 登记与发布共用同一套编码校验规则（validate_related_dims），结论一致
- 事实发布只能引用存在且已确认（已发布）的维度，错误消息指出具体编码
- 失败提交不覆盖上次有效粒度、不移除有效引用、不产生下游消息
- 度量建议来自发布接口最终确认的定义，携带实际生效的粒度和维度组合
- 同一份校验记录在事实登记入口与度量配置工作台可互相核对
"""
from __future__ import annotations

import pytest

from app.store import Store
from app.services import base as base_module
from app.services import fact_design as fact_design_module
from app.services import measure_config as measure_config_module
from app.services.fact_design import (
    FactDesignService,
    parse_dim_codes,
    validate_related_dims,
)
from app.services.measure_config import MeasureConfigService


@pytest.fixture
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> Store:
    """每个测试使用独立 store，替换服务模块引用的全局仓库。"""
    fresh = Store()
    monkeypatch.setattr(base_module, "store", fresh)
    monkeypatch.setattr(fact_design_module, "store", fresh)
    monkeypatch.setattr(measure_config_module, "store", fresh)
    return fresh


@pytest.fixture
def fact_service() -> FactDesignService:
    return FactDesignService()


@pytest.fixture
def measure_service() -> MeasureConfigService:
    return MeasureConfigService()


def _fact_values(**overrides: str) -> dict[str, str]:
    values = {
        "事实名称": "测试事实",
        "业务过程": "测试业务过程",
        "粒度声明": "每笔记测试明细",
        "关联维度": "DIM_TIME.DIM_PRODUCT",
        "度量字段": "sales_amt.sales_qty",
        "分区字段": "dt DATE",
        "更新模式": "每日追加",
    }
    values.update(overrides)
    return values


def _channel_payloads(store: Store, topic: str) -> list[dict]:
    return [msg["payload"] for msg in store.channel.topic_snapshot(topic)]


# ======================================================================
# Section A: 登记入口拦截混合关联维度
# ======================================================================


class TestRegistrationValidation:
    """登记阶段：含不存在编码的定义不得保存。"""

    def test_mixed_dims_registration_rejected(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """DIM_TIME 有效、DIM_MISSING 不存在：整个定义不得保存。"""
        before = len(fresh_store.rows("fact_design"))
        entry, missing = fact_service.create_entry(
            _fact_values(关联维度="DIM_TIME.DIM_MISSING"))
        assert entry is None
        assert missing
        # 错误消息指出哪个编码有问题
        assert any("DIM_MISSING" in m for m in missing)
        assert len(fresh_store.rows("fact_design")) == before

    def test_mixed_dims_not_leaked_to_channel(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """失败提交不得发布 fact.defined，避免覆盖下游上次有效粒度。"""
        before = len(_channel_payloads(fresh_store, "fact.defined"))
        entry, _ = fact_service.create_entry(_fact_values(关联维度="DIM_TIME.DIM_MISSING"))
        assert entry is None
        assert len(_channel_payloads(fresh_store, "fact.defined")) == before

    def test_failed_registration_leaves_validation_record(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """失败提交也要留下可追溯的校验记录，指明失败字段。"""
        fact_service.create_entry(_fact_values(关联维度="DIM_TIME.DIM_MISSING"))
        records = _channel_payloads(fresh_store, "fact.dim_validated")
        assert len(records) == 1
        record = records[0]
        assert record["valid"] is False
        assert record["invalid_codes"] == ["DIM_MISSING"]
        check_by_code = {c["code"]: c for c in record["checks"]}
        assert check_by_code["DIM_TIME"]["valid"] is True
        assert check_by_code["DIM_MISSING"]["exists"] is False

    def test_valid_registration_saved_with_record(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """全有效定义正常登记，校验记录挂在事实上。"""
        entry, missing = fact_service.create_entry(_fact_values())
        assert missing == []
        assert entry is not None
        record = entry["_dim_validation"]
        assert record["valid"] is True
        assert record["codes"] == ["DIM_TIME", "DIM_PRODUCT"]
        assert record["invalid_codes"] == []
        # fact.defined 携带同一份校验记录
        defined = _channel_payloads(fresh_store, "fact.defined")
        assert defined[-1]["dim_validation"]["record_id"] == record["record_id"]


# ======================================================================
# Section B: 登记与发布共用编码校验规则
# ======================================================================


class TestSharedValidationRule:
    """同一套编码写法与同一份校验规则贯穿登记与发布。"""

    def test_parse_dim_codes_unified_notation(self) -> None:
        """统一写法：点分隔、去空白、忽略空段。"""
        assert parse_dim_codes(" DIM_TIME. DIM_PRODUCT..DIM_STORE ") == [
            "DIM_TIME", "DIM_PRODUCT", "DIM_STORE"]
        assert parse_dim_codes("") == []
        assert parse_dim_codes(None) == []

    def test_validate_related_dims_points_out_bad_codes(self, fresh_store: Store) -> None:
        """校验结论逐编码给出，原始输入里哪个编码有问题一目了然。"""
        result = validate_related_dims("DIM_TIME.DIM_MISSING.DIM_CHANNEL")
        assert result["valid"] is False
        assert result["missing_codes"] == ["DIM_MISSING"]
        assert result["unconfirmed_codes"] == ["DIM_CHANNEL"]
        assert "DIM_MISSING" in result["message"]
        assert "DIM_CHANNEL" in result["message"]
        check_by_code = {c["code"]: c for c in result["checks"]}
        assert check_by_code["DIM_TIME"]["confirmed"] is True
        assert check_by_code["DIM_CHANNEL"]["confirmed"] is False

    def test_same_input_same_record_fingerprint(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """同一份输入在登记与发布两步得到同一份校验记录（指纹一致）。"""
        entry, _ = fact_service.create_entry(_fact_values())
        assert entry is not None
        register_record = entry["_dim_validation"]
        published, message = fact_service.run_action(entry["id"], "发布事实")
        assert published is not None, message
        publish_record = published["_dim_validation"]
        assert publish_record["fingerprint"] == register_record["fingerprint"]
        assert publish_record["codes"] == register_record["codes"]
        assert publish_record["valid"] is True


# ======================================================================
# Section C: 发布入口校验
# ======================================================================


class TestPublishValidation:
    """发布阶段：只能引用存在且已确认的维度。"""

    def test_publish_rejects_unconfirmed_dim(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """DIM_CHANNEL 存在但仍为草稿：登记可追溯，发布必须拦截。"""
        entry, missing = fact_service.create_entry(
            _fact_values(关联维度="DIM_TIME.DIM_CHANNEL"))
        assert entry is not None, f"存在但未确认的维度允许先登记：{missing}"
        # 登记工作台的校验记录已经能追到问题字段
        record = entry["_dim_validation"]
        assert record["valid"] is False
        assert record["invalid_codes"] == ["DIM_CHANNEL"]

        published, message = fact_service.run_action(entry["id"], "发布事实")
        assert published is None
        assert "DIM_CHANNEL" in message
        assert "未确认" in message

    def test_failed_publish_keeps_last_valid_state(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """失败提交不覆盖上次有效粒度、不移除有效引用、不改动状态。"""
        entry, _ = fact_service.create_entry(_fact_values())
        assert entry is not None
        fact_service.run_action(entry["id"], "提交设计")
        status_before = entry["status"]
        granularity_before = entry["粒度声明"]
        dims_before = entry["关联维度"]

        # 维度在发布前被停用，发布应失败
        for row in fresh_store.rows("dim_definition"):
            if row.get("维度编码") == "DIM_PRODUCT":
                row["status"] = "已停用"
        published, message = fact_service.run_action(entry["id"], "发布事实")
        assert published is None
        assert "DIM_PRODUCT" in message
        assert entry["status"] == status_before
        assert entry["粒度声明"] == granularity_before
        assert entry["关联维度"] == dims_before

    def test_failed_publish_emits_no_downstream_messages(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """待修输入不得生成可用口径：不发布 fact.published / measure.suggested。"""
        entry, _ = fact_service.create_entry(
            _fact_values(关联维度="DIM_TIME.DIM_CHANNEL"))
        assert entry is not None
        published, _ = fact_service.run_action(entry["id"], "发布事实")
        assert published is None
        assert _channel_payloads(fresh_store, "fact.published") == []
        assert _channel_payloads(fresh_store, "measure.suggested") == []

    def test_publish_requires_at_least_one_dim(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """关联维度为空的事实不得发布。"""
        entry, _ = fact_service.create_entry(_fact_values(关联维度=""))
        assert entry is not None
        published, message = fact_service.run_action(entry["id"], "发布事实")
        assert published is None
        assert "至少" in message
        assert entry["status"] == "草稿"


# ======================================================================
# Section D: 度量建议来自发布确认的定义
# ======================================================================


class TestMeasureSuggestion:
    """度量配置从发布接口最终确认的事实字段定义生成建议。"""

    def _publish_fact(self, fact_service: FactDesignService) -> dict:
        entry, _ = fact_service.create_entry(_fact_values())
        assert entry is not None
        published, message = fact_service.run_action(entry["id"], "发布事实")
        assert published is not None, message
        return published

    def test_suggestions_carry_effective_granularity_and_dims(
        self, fresh_store: Store, fact_service: FactDesignService
    ) -> None:
        """每条建议对应实际生效的事实粒度和维度组合。"""
        published = self._publish_fact(fact_service)
        suggestions = _channel_payloads(fresh_store, "measure.suggested")
        assert len(suggestions) == 2  # sales_amt.sales_qty
        for s in suggestions:
            assert s["fact_name"] == published["事实名称"]
            assert s["granularity"] == published["粒度声明"]
            assert s["related_dims"] == published["关联维度"]
            assert s["dim_validated"] is True
            assert s["dim_validation_id"] == published["_dim_validation"]["record_id"]

    def test_measure_config_applies_validated_suggestion(
        self, fresh_store: Store, fact_service: FactDesignService,
        measure_service: MeasureConfigService,
    ) -> None:
        """度量配置采纳建议，并把粒度、维度组合与同一份校验记录持久化。"""
        published = self._publish_fact(fact_service)
        measure, missing = measure_service.create_entry({
            "度量名称": "sales_amt",
            "所属事实": published["事实名称"],
            "度量类型": "",
            "聚合方式": "",
            "精度要求": "",
            "是否可加": "",
            "来源字段": "sales_amt",
        })
        assert missing == []
        assert measure is not None
        assert measure["度量类型"] == "DECIMAL(18,2)"
        assert measure["聚合方式"] == "SUM"
        # 建议对应实际生效的粒度与维度组合
        assert measure["_suggested_granularity"] == published["粒度声明"]
        assert measure["_suggested_dims"] == published["关联维度"]
        # 与事实登记入口核对同一份校验记录
        assert measure["_fact_dim_validation"]["record_id"] == \
            published["_dim_validation"]["record_id"]
        assert measure["_fact_dim_validation"]["fingerprint"] == \
            published["_dim_validation"]["fingerprint"]

    def test_measure_config_sees_failed_record_too(
        self, fresh_store: Store, fact_service: FactDesignService,
        measure_service: MeasureConfigService,
    ) -> None:
        """先提交全有效定义，再提交混合关联维度：
        度量配置工作台能核对到同一份（含失败的）校验记录。"""
        # 先提交全有效定义
        ok_entry, _ = fact_service.create_entry(_fact_values(事实名称="全有效事实"))
        assert ok_entry is not None
        # 再提交混合关联维度（被拦截）
        bad_entry, missing = fact_service.create_entry(
            _fact_values(事实名称="混合事实", 关联维度="DIM_TIME.DIM_MISSING"))
        assert bad_entry is None
        assert any("DIM_MISSING" in m for m in missing)

        # 度量配置工作台核对同一份校验记录
        snapshot = measure_service.domain_snapshot()
        records = snapshot["fact_dim_validation_records"]
        by_fact = {r["fact_name"]: r for r in records}
        assert by_fact["全有效事实"]["valid"] is True
        assert by_fact["混合事实"]["valid"] is False
        assert by_fact["混合事实"]["invalid_codes"] == ["DIM_MISSING"]
        assert snapshot["fact_dim_validation_failed"] >= 1

        # 事实登记入口看到的是同一份记录
        fact_snapshot = fact_service.domain_snapshot()
        fact_records = {r["fact_name"]: r for r in fact_snapshot["dim_validation_records"]}
        assert fact_records["混合事实"]["record_id"] == by_fact["混合事实"]["record_id"]
        assert fact_records["全有效事实"]["record_id"] == by_fact["全有效事实"]["record_id"]

    def test_unvalidated_fact_generates_no_usable_measure(
        self, fresh_store: Store, fact_service: FactDesignService,
        measure_service: MeasureConfigService,
    ) -> None:
        """待修输入不得生成可用口径：未通过校验的定义没有建议可消费。"""
        entry, _ = fact_service.create_entry(
            _fact_values(事实名称="待修事实", 关联维度="DIM_TIME.DIM_CHANNEL"))
        assert entry is not None
        published, _ = fact_service.run_action(entry["id"], "发布事实")
        assert published is None

        measure, _ = measure_service.create_entry({
            "度量名称": "sales_amt",
            "所属事实": "待修事实",
            "度量类型": "",
            "聚合方式": "",
            "精度要求": "",
            "是否可加": "",
            "来源字段": "sales_amt",
        })
        assert measure is not None
        # 没有来自发布确认定义的建议，口径保持命名推断而非被污染
        assert "_suggested_granularity" not in measure
        assert "_suggested_dims" not in measure
        assert measure["_fact_dim_validation"]["valid"] is False
