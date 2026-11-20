"""事实表设计服务：管理事实表的业务过程、粒度和维度关联定义。

增强能力：
- 事实表名称标准化校验（前缀fact_）
- 关联维度编码统一校验：登记与发布共用同一套编码校验规则（validate_related_dims），
  编码必须存在于 dim_definition；事实发布进一步要求维度已确认（已发布）。
  校验记录可追溯具体失败编码，并同步到 fact.dim_validated 通道供下游核对
- 消费 dim.published 通道确认关联维度可用
- 发布事实时触发度量配置和聚合层自动建议，建议携带实际生效的粒度与维度组合
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 关联维度编码的统一写法：多个编码以 '.' 分隔，登记与发布两步一致
DIM_CODE_SEPARATOR = "."
# 维度「已确认」的判定状态：只有已发布的维度才允许被事实发布引用
DIM_CONFIRMED_STATUS = "已发布"


class FactDesignService(BaseModuleService):
    MODULE = "fact_design"
    ENTITY = "事实表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["事实名称", "业务过程", "粒度声明", "关联维度", "度量字段", "分区字段", "更新模式"]
    REQUIRED_FIELDS = ["事实名称", "业务过程", "粒度声明"]
    STATUS_ORDER = ["草稿", "已设计", "已发布", "已冻结"]
    ACTION_RULES = {"提交设计": "草稿", "发布事实": "已设计", "冻结事实": "已发布"}
    NEGATIVE_ACTIONS = []

    UPDATE_MODES = {"每日追加", "每小时追加", "T+1批量", "实时追加", "每日全量"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建事实表定义：登记与发布共用编码校验规则，拦截不存在的维度编码。"""
        # 标准化事实名称
        fact_name = str(values.get("事实名称", "")).strip()
        values["事实名称"] = fact_name

        # 标准化更新模式
        update_mode = str(values.get("更新模式", "每日追加")).strip()
        if update_mode not in self.UPDATE_MODES:
            update_mode = "每日追加"
        values["更新模式"] = update_mode

        # 共用编码校验规则：登记阶段拦截不存在的维度编码，
        # 含错误编码的定义不保存、不发布，避免覆盖下游上次有效粒度
        related_dims = str(values.get("关联维度", "")).strip()
        validation = validate_related_dims(related_dims)
        if validation["missing_codes"]:
            record = _build_validation_record(None, fact_name, related_dims, validation, stage="登记")
            store.channel.publish("fact.dim_validated", record)
            return None, [
                f"关联维度包含未定义的维度编码：{'、'.join(validation['missing_codes'])}"
            ]

        # 消费已发布维度消息，确认维度可用
        dim_msgs = store.channel.consume("dim.published", "fact_design")
        confirmed_via_channel = {
            str(msg["payload"].get("dim_code", "")) for msg in dim_msgs
        }

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 同一份校验记录挂在事实定义上，登记工作台可追溯到具体失败字段
        record = _build_validation_record(entry["id"], fact_name, related_dims, validation, stage="登记")
        record["channel_confirmed"] = sorted(
            code for code in validation["codes"] if code in confirmed_via_channel
        )
        entry["_dim_validation"] = record
        store.channel.publish("fact.dim_validated", record)

        # 发布事实表定义到数据通道（只有通过编码校验的定义才允许下游消费）
        store.channel.publish("fact.defined", {
            "fact_name": entry["事实名称"],
            "business_process": entry["业务过程"],
            "granularity": entry["粒度声明"],
            "related_dims": related_dims,
            "measures": entry.get("度量字段", ""),
            "partition_field": entry.get("分区字段", ""),
            "update_mode": entry["更新模式"],
            "dim_validation": record,
            "entry_id": entry["id"],
        })

        # 注册事实产物
        store.artifacts.register(
            "fact_design", "fact_table",
            f"fact_{entry['id']}",
            {"fact_name": entry["事实名称"], "business_process": entry["业务过程"]},
            source_modules=["dim_definition", "fact_design"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发布事实":
            # 发布前先走共用编码校验规则：只能引用存在且已确认的维度。
            # 校验失败不改动状态、不覆盖粒度、不移除有效引用、不通知下游。
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            related_dims = str(entry.get("关联维度", "")).strip()
            validation = validate_related_dims(related_dims)
            record = _build_validation_record(
                entry_id, str(entry.get("事实名称", "")), related_dims, validation, stage="发布")
            # 校验记录同步到登记工作台与数据通道，失败字段可追溯
            entry["_dim_validation"] = record
            store.channel.publish("fact.dim_validated", record)
            if not validation["valid"]:
                return None, validation["message"]

        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布事实":
            store.channel.publish("fact.published", {
                "fact_name": entry["事实名称"],
                "business_process": entry["业务过程"],
                "granularity": entry["粒度声明"],
                "related_dims": entry.get("关联维度", ""),
                "measures": entry.get("度量字段", ""),
                "update_mode": entry.get("更新模式"),
                "dim_validation": entry.get("_dim_validation"),
                "entry_id": entry["id"],
            })

            # 度量建议从发布接口最终确认的事实字段定义生成
            _suggest_measures(entry)
            # 自动建议聚合汇总
            _suggest_aggregation(entry)

        elif action == "冻结事实":
            store.channel.publish("fact.frozen", {
                "fact_name": entry["事实名称"],
                "entry_id": entry["id"],
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        dim_consumed = len(store.channel.consume("dim.published", "fact_design"))
        base["consumed_published_dims"] = dim_consumed

        # 维度关联矩阵（与校验规则同一套编码写法）
        dim_matrix: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            fact_name = row.get("事实名称", "")
            dim_matrix[fact_name] = parse_dim_codes(row.get("关联维度", ""))
        base["dimension_association_matrix"] = dim_matrix

        # 最近的关联维度校验记录，供登记工作台与下游模块核对同一份结论
        records = [msg["payload"] for msg in store.channel.topic_snapshot("fact.dim_validated")]
        base["dim_validation_records"] = records[-10:]
        base["dim_validation_failed"] = sum(1 for r in records if not r.get("valid"))
        return base


def parse_dim_codes(raw: Any) -> list[str]:
    """统一编码写法：按 '.' 分隔、去除空白、忽略空段。"""
    return [seg.strip() for seg in str(raw or "").split(DIM_CODE_SEPARATOR) if seg.strip()]


def validate_related_dims(raw: Any) -> dict[str, Any]:
    """登记与发布共用的关联维度编码校验规则。

    每个编码逐一核对 dim_definition：
    - exists：编码已定义（登记阶段据此拦截）
    - confirmed：维度已确认（已发布），事实发布只允许引用此类维度
    返回结构化结论，invalid_codes 直接指出原始输入里哪个编码有问题。
    """
    codes = parse_dim_codes(raw)
    dim_rows = {str(row.get("维度编码", "")): row for row in store.rows("dim_definition")}
    checks: list[dict[str, Any]] = []
    for code in codes:
        row = dim_rows.get(code)
        if row is None:
            checks.append({"code": code, "exists": False, "confirmed": False,
                           "valid": False, "reason": "不存在"})
        elif row.get("status") != DIM_CONFIRMED_STATUS:
            checks.append({"code": code, "exists": True, "confirmed": False, "valid": False,
                           "reason": f"未确认（当前状态：{row.get('status', '未知')}）"})
        else:
            checks.append({"code": code, "exists": True, "confirmed": True,
                           "valid": True, "reason": ""})
    missing_codes = [c["code"] for c in checks if not c["exists"]]
    unconfirmed_codes = [c["code"] for c in checks if c["exists"] and not c["confirmed"]]
    if not codes:
        message = "事实表至少需要关联一个维度"
    else:
        message = "；".join(
            f"维度编码 {c['code']} {c['reason']}" for c in checks if not c["valid"])
    return {
        "valid": bool(codes) and not missing_codes and not unconfirmed_codes,
        "codes": codes,
        "checks": checks,
        "missing_codes": missing_codes,
        "unconfirmed_codes": unconfirmed_codes,
        "invalid_codes": missing_codes + unconfirmed_codes,
        "message": message,
    }


def _build_validation_record(entry_id: int | None, fact_name: str, raw_input: str,
                             validation: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """生成可追溯的关联维度校验记录。

    同一份输入在登记与发布两侧的指纹一致，事实登记入口与度量配置工作台
    核对的是同一份校验记录。
    """
    fingerprint = hashlib.sha256(
        f"{fact_name}|{raw_input}|{DIM_CODE_SEPARATOR.join(validation['codes'])}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "record_id": f"dimval-{uuid.uuid4().hex[:8]}",
        "fingerprint": fingerprint,
        "stage": stage,
        "entry_id": entry_id,
        "fact_name": fact_name,
        "raw_input": raw_input,
        "codes": list(validation["codes"]),
        "checks": [dict(c) for c in validation["checks"]],
        "valid": validation["valid"],
        "invalid_codes": list(validation["invalid_codes"]),
        "message": validation["message"],
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def _suggest_measures(fact: dict[str, Any]) -> None:
    """基于发布接口最终确认的事实定义生成度量建议。

    每条建议携带实际生效的事实粒度与维度组合；
    未通过校验（待修）的定义不会走到这里，也就不会生成可用口径。
    """
    measures_str = str(fact.get("度量字段", "")).strip()
    if not measures_str:
        return

    validation = fact.get("_dim_validation") or {}
    measure_names = [m.strip() for m in measures_str.split(DIM_CODE_SEPARATOR) if m.strip()]
    for m_name in measure_names:
        measure_type = "DECIMAL(18,2)" if "_amt" in m_name else "BIGINT"
        store.channel.publish("measure.suggested", {
            "measure_name": m_name,
            "fact_name": fact["事实名称"],
            "measure_type": measure_type,
            "agg_method": "SUM" if ("_amt" in m_name or "_qty" in m_name) else "COUNT",
            "source_fact_id": fact.get("id"),
            "is_additive": "_cnt" not in m_name and "_rate" not in m_name,
            "granularity": fact.get("粒度声明", ""),
            "related_dims": fact.get("关联维度", ""),
            "dim_validation_id": validation.get("record_id"),
            "dim_validated": bool(validation.get("valid")),
        })


def _suggest_aggregation(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议汇总表配置。"""
    fact_name = fact.get("事实名称", "")
    store.channel.publish("agg.suggested", {
        "agg_name": f"每日{fact_name}汇总",
        "source_fact": fact_name,
        "agg_dims": fact.get("关联维度", ""),
        "agg_measures": fact.get("度量字段", ""),
        "agg_granularity": "每日",
        "source_fact_id": fact.get("id"),
    })
