"""CASE.json 数据模型与结构校验。

校验通过后产出不可变（按约定）的 :class:`Case`，其中所有集合均以整数位掩码
表示：原子类别按 ``atoms`` 中的排序位置分配 bit 位，标签、对象、角色全部复用
同一张位表，因此标签并集、子集判定、超标计数都退化成位运算（见 planner 模块
对掩码复用的说明）。

所有校验函数在出错时抛出带 JSONPath 位置的 :class:`~solution.errors.ValidationError`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    E_DUPLICATE,
    E_EMPTY,
    E_MISSING,
    E_RANGE,
    E_REF,
    E_TYPE,
    E_UNKNOWN,
    ValidationError,
)

# 题目给定的规模上限
MAX_OLD_LABELS = 16
MAX_CANDIDATES_PER_LABEL = 3

# 允许出现在顶层的字段
_TOP_FIELDS = {
    "case_id", "now", "old_version", "new_version", "atoms",
    "old_labels", "new_labels", "mappings", "objects", "roles", "plan",
}
_LABEL_FIELDS = {"id", "atoms"}
_MAPPING_FIELDS = {"old_label", "candidates"}
_CANDIDATE_FIELDS = {"id", "new_labels"}
_OBJECT_FIELDS = {"id", "labels"}
_ROLE_FIELDS = {"id", "authorized_atoms"}
_PLAN_FIELDS = {"plan_id", "created_at", "valid_until", "selection"}
_SELECTION_FIELDS = {"old_label", "candidate_id"}


# ---------------------------------------------------------------------------
# 基础类型校验助手
# ---------------------------------------------------------------------------
def _require_type(value, expected: "type | tuple[type, ...]", path: str, name: str):
    if not isinstance(value, expected):
        if isinstance(expected, tuple):
            want = "/".join(t.__name__ for t in expected)
        else:
            want = expected.__name__
        raise ValidationError(
            E_TYPE, f"字段 {name} 应为 {want}，实际为 {type(value).__name__}", path
        )


def _require_object(value, path: str):
    if not isinstance(value, dict):
        raise ValidationError(E_TYPE, f"应为 JSON 对象，实际为 {type(value).__name__}", path)


def _check_known_keys(obj: dict, allowed: set[str], path: str):
    for key in obj:
        if key not in allowed:
            raise ValidationError(E_UNKNOWN, f"未声明的字段 {key!r}", f"{path}.{key}")


def _require_keys(obj: dict, keys: list[str], path: str):
    for key in keys:
        if key not in obj:
            raise ValidationError(E_MISSING, f"缺少必填字段 {key!r}", f"{path}.{key}")


def _nonempty_str(value, path: str, name: str) -> str:
    _require_type(value, str, path, name)
    if value == "":
        raise ValidationError(E_EMPTY, f"字段 {name} 不允许为空字符串", path)
    return value


def _nonnegative_int(value, path: str, name: str) -> int:
    _require_type(value, int, path, name)
    if isinstance(value, bool) or value < 0:
        raise ValidationError(E_RANGE, f"字段 {name} 应为非负整数", path)
    return value


def _string_list(value, path: str, name: str) -> list[str]:
    _require_type(value, list, path, name)
    if not value:
        raise ValidationError(E_EMPTY, f"字段 {name} 不允许为空列表", path)
    out: list[str] = []
    for i, item in enumerate(value):
        sub = f"{path}[{i}]"
        out.append(_nonempty_str(item, sub, name))
    return out


# ---------------------------------------------------------------------------
# 解析后的领域模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Label:
    id: str
    atom_mask: int
    atom_ids: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    id: str
    new_label_ids: tuple[str, ...]
    atom_mask: int          # 候选映射覆盖的新原子并集（掩码）
    width: int              # 使用的新标签个数（第二优化目标按次计）


@dataclass(frozen=True)
class Mapping:
    old_label: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class Obj:
    id: str
    old_label_ids: tuple[str, ...]
    old_mask: int           # 原有效敏感集合 = 旧标签并集


@dataclass(frozen=True)
class Role:
    id: str
    auth_mask: int
    auth_atom_ids: tuple[str, ...]


@dataclass(frozen=True)
class Selection:
    old_label: str
    candidate_id: str


@dataclass(frozen=True)
class PlanBlock:
    plan_id: str
    created_at: int
    valid_until: int
    selection: tuple[Selection, ...]


@dataclass(frozen=True)
class Case:
    case_id: str
    now: int
    old_version: str
    new_version: str
    atoms: tuple[str, ...]
    bit_index: dict[str, int]
    old_labels: tuple[Label, ...]
    new_labels: tuple[Label, ...]
    mappings: tuple[Mapping, ...]
    objects: tuple[Obj, ...]
    roles: tuple[Role, ...]
    plan: PlanBlock | None
    # 便于按 ID 查掩码的索引
    old_label_map: dict[str, Label] = field(default_factory=dict, compare=False)
    new_label_map: dict[str, Label] = field(default_factory=dict, compare=False)
    mapping_map: dict[str, Mapping] = field(default_factory=dict, compare=False)
    object_map: dict[str, Obj] = field(default_factory=dict, compare=False)
    role_map: dict[str, Role] = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------------------
# 各区块解析
# ---------------------------------------------------------------------------
def _parse_labels(block, path: str, atom_index: dict[str, int]) -> tuple[Label, ...]:
    _require_type(block, list, path, path[2:])
    if not block:
        raise ValidationError(E_EMPTY, "标签列表不允许为空", path)
    labels: list[Label] = []
    seen: set[str] = set()
    for i, raw in enumerate(block):
        sub = f"{path}[{i}]"
        _require_object(raw, sub)
        _check_known_keys(raw, _LABEL_FIELDS, sub)
        _require_keys(raw, ["id", "atoms"], sub)
        lid = _nonempty_str(raw["id"], f"{sub}.id", "id")
        if lid in seen:
            raise ValidationError(E_DUPLICATE, f"标签 ID {lid!r} 重复", f"{sub}.id")
        seen.add(lid)
        atom_ids = _string_list(raw["atoms"], f"{sub}.atoms", "atoms")
        mask = 0
        for j, aid in enumerate(atom_ids):
            if aid not in atom_index:
                raise ValidationError(
                    E_REF, f"原子类别 {aid!r} 未在 $.atoms 中登记", f"{sub}.atoms[{j}]"
                )
            bit = 1 << atom_index[aid]
            if mask & bit:  # 同一标签内原子不可重复
                raise ValidationError(
                    E_DUPLICATE, f"标签 {lid!r} 内原子类别 {aid!r} 重复",
                    f"{sub}.atoms[{j}]",
                )
            mask |= bit
        labels.append(Label(lid, mask, tuple(atom_ids)))
    return tuple(sorted(labels, key=lambda x: x.id))


def _parse_mappings(block, old_ids: set[str], new_label_map: dict[str, Label],
                    path: str) -> tuple[Mapping, ...]:
    _require_type(block, list, path, "mappings")
    if not block:
        raise ValidationError(E_EMPTY, "mappings 不允许为空列表", path)
    mappings: list[Mapping] = []
    seen_old: set[str] = set()
    for i, raw in enumerate(block):
        sub = f"{path}[{i}]"
        _require_object(raw, sub)
        _check_known_keys(raw, _MAPPING_FIELDS, sub)
        _require_keys(raw, ["old_label", "candidates"], sub)
        old_label = _nonempty_str(raw["old_label"], f"{sub}.old_label", "old_label")
        if old_label not in old_ids:
            raise ValidationError(
                E_REF, f"旧标签 {old_label!r} 未在 $.old_labels 中登记",
                f"{sub}.old_label",
            )
        if old_label in seen_old:
            raise ValidationError(
                E_DUPLICATE, f"旧标签 {old_label!r} 存在多条映射", f"{sub}.old_label"
            )
        seen_old.add(old_label)

        cands_raw = raw["candidates"]
        _require_type(cands_raw, list, f"{sub}.candidates", "candidates")
        if not cands_raw:
            raise ValidationError(E_EMPTY, "每个旧标签至少需要 1 个候选映射",
                                  f"{sub}.candidates")
        if len(cands_raw) > MAX_CANDIDATES_PER_LABEL:
            raise ValidationError(
                E_RANGE,
                f"每个旧标签至多 {MAX_CANDIDATES_PER_LABEL} 个候选映射，"
                f"实际 {len(cands_raw)} 个",
                f"{sub}.candidates",
            )
        seen_cid: set[str] = set()
        cands: list[Candidate] = []
        for j, craw in enumerate(cands_raw):
            csub = f"{sub}.candidates[{j}]"
            _require_object(craw, csub)
            _check_known_keys(craw, _CANDIDATE_FIELDS, csub)
            _require_keys(craw, ["id", "new_labels"], csub)
            cid = _nonempty_str(craw["id"], f"{csub}.id", "id")
            if cid in seen_cid:
                raise ValidationError(E_DUPLICATE,
                                      f"候选 ID {cid!r} 在旧标签 {old_label!r} 下重复",
                                      f"{csub}.id")
            seen_cid.add(cid)
            nl_ids = _string_list(craw["new_labels"], f"{csub}.new_labels",
                                  "new_labels")
            mask = 0
            for k, nlid in enumerate(nl_ids):
                if nlid not in new_label_map:
                    raise ValidationError(
                        E_REF, f"新标签 {nlid!r} 未在 $.new_labels 中登记",
                        f"{csub}.new_labels[{k}]",
                    )
                # 新标签间允许原子集合重叠；掩码 OR 自动去重，
                # 宽度仍按显式列出的新标签个数计（同一新标签不得重复列出）。
                if nlid in nl_ids[:k]:
                    raise ValidationError(
                        E_DUPLICATE,
                        f"候选 {cid!r} 中新标签 {nlid!r} 重复列出",
                        f"{csub}.new_labels[{k}]",
                    )
                mask |= new_label_map[nlid].atom_mask
            cands.append(Candidate(cid, tuple(nl_ids), mask, len(nl_ids)))
        mappings.append(Mapping(old_label, tuple(cands)))

    missing = old_ids - seen_old
    if missing:
        raise ValidationError(
            E_MISSING,
            f"旧标签 {sorted(missing)!r} 缺少候选映射（每个旧标签必须声明映射）",
            path,
        )
    return tuple(sorted(mappings, key=lambda m: m.old_label))


def _parse_objects(block, old_label_map: dict[str, Label], path: str) -> tuple[Obj, ...]:
    _require_type(block, list, path, "objects")
    if not block:
        raise ValidationError(E_EMPTY, "objects 不允许为空列表", path)
    objs: list[Obj] = []
    seen: set[str] = set()
    for i, raw in enumerate(block):
        sub = f"{path}[{i}]"
        _require_object(raw, sub)
        _check_known_keys(raw, _OBJECT_FIELDS, sub)
        _require_keys(raw, ["id", "labels"], sub)
        oid = _nonempty_str(raw["id"], f"{sub}.id", "id")
        if oid in seen:
            raise ValidationError(E_DUPLICATE, f"对象 ID {oid!r} 重复", f"{sub}.id")
        seen.add(oid)
        label_ids = _string_list(raw["labels"], f"{sub}.labels", "labels")
        mask = 0
        for j, lid in enumerate(label_ids):
            if lid not in old_label_map:
                raise ValidationError(
                    E_REF, f"旧标签 {lid!r} 未在 $.old_labels 中登记",
                    f"{sub}.labels[{j}]",
                )
            if lid in label_ids[:j]:
                raise ValidationError(
                    E_DUPLICATE, f"对象 {oid!r} 中标签 {lid!r} 重复列出",
                    f"{sub}.labels[{j}]",
                )
            mask |= old_label_map[lid].atom_mask
        objs.append(Obj(oid, tuple(label_ids), mask))
    return tuple(sorted(objs, key=lambda o: o.id))


def _parse_roles(block, atom_index: dict[str, int], path: str) -> tuple[Role, ...]:
    _require_type(block, list, path, "roles")
    if not block:
        raise ValidationError(E_EMPTY, "roles 不允许为空列表（至少登记一个角色）", path)
    roles: list[Role] = []
    seen: set[str] = set()
    for i, raw in enumerate(block):
        sub = f"{path}[{i}]"
        _require_object(raw, sub)
        _check_known_keys(raw, _ROLE_FIELDS, sub)
        _require_keys(raw, ["id", "authorized_atoms"], sub)
        rid = _nonempty_str(raw["id"], f"{sub}.id", "id")
        if rid in seen:
            raise ValidationError(E_DUPLICATE, f"角色 ID {rid!r} 重复", f"{sub}.id")
        seen.add(rid)
        # 角色授权集合允许为空（无权读取任何含原子类别的对象）
        atoms = raw["authorized_atoms"]
        _require_type(atoms, list, f"{sub}.authorized_atoms", "authorized_atoms")
        mask = 0
        for j, aid in enumerate(atoms):
            if not isinstance(aid, str) or aid == "":
                raise ValidationError(E_TYPE, "原子类别 ID 应为非空字符串",
                                      f"{sub}.authorized_atoms[{j}]")
            if aid not in atom_index:
                raise ValidationError(
                    E_REF, f"原子类别 {aid!r} 未在 $.atoms 中登记",
                    f"{sub}.authorized_atoms[{j}]",
                )
            bit = 1 << atom_index[aid]
            if mask & bit:
                raise ValidationError(E_DUPLICATE,
                                      f"角色 {rid!r} 授权原子 {aid!r} 重复",
                                      f"{sub}.authorized_atoms[{j}]")
            mask |= bit
        roles.append(Role(rid, mask, tuple(atoms)))
    return tuple(sorted(roles, key=lambda r: r.id))


def _parse_plan(block, mapping_map: dict[str, Mapping], path: str) -> PlanBlock:
    _require_object(block, path)
    _check_known_keys(block, _PLAN_FIELDS, path)
    _require_keys(block, ["plan_id", "created_at", "valid_until", "selection"], path)
    plan_id = _nonempty_str(block["plan_id"], f"{path}.plan_id", "plan_id")
    created_at = _nonnegative_int(block["created_at"], f"{path}.created_at",
                                  "created_at")
    valid_until = _nonnegative_int(block["valid_until"], f"{path}.valid_until",
                                   "valid_until")
    if valid_until < created_at:
        raise ValidationError(E_RANGE, "valid_until 不能早于 created_at",
                              f"{path}.valid_until")
    sel_raw = block["selection"]
    _require_type(sel_raw, list, f"{path}.selection", "selection")
    if not sel_raw:
        raise ValidationError(E_EMPTY, "plan.selection 不允许为空", f"{path}.selection")
    seen_old: set[str] = set()
    sels: list[Selection] = []
    for i, raw in enumerate(sel_raw):
        sub = f"{path}.selection[{i}]"
        _require_object(raw, sub)
        _check_known_keys(raw, _SELECTION_FIELDS, sub)
        _require_keys(raw, ["old_label", "candidate_id"], sub)
        old_label = _nonempty_str(raw["old_label"], f"{sub}.old_label", "old_label")
        if old_label not in mapping_map:
            raise ValidationError(E_REF,
                                  f"旧标签 {old_label!r} 未在 $.mappings 中登记",
                                  f"{sub}.old_label")
        if old_label in seen_old:
            raise ValidationError(E_DUPLICATE,
                                  f"计划中旧标签 {old_label!r} 出现多次",
                                  f"{sub}.old_label")
        seen_old.add(old_label)
        cid = _nonempty_str(raw["candidate_id"], f"{sub}.candidate_id",
                            "candidate_id")
        valid_ids = {c.id for c in mapping_map[old_label].candidates}
        if cid not in valid_ids:
            raise ValidationError(
                E_REF,
                f"候选 {cid!r} 不是旧标签 {old_label!r} 的合法候选"
                f"（合法值：{sorted(valid_ids)!r}）",
                f"{sub}.candidate_id",
            )
        sels.append(Selection(old_label, cid))
    missing = set(mapping_map) - seen_old
    if missing:
        raise ValidationError(E_MISSING,
                              f"计划缺少旧标签 {sorted(missing)!r} 的映射选择",
                              f"{path}.selection")
    return PlanBlock(plan_id, created_at, valid_until,
                     tuple(sorted(sels, key=lambda s: s.old_label)))


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def parse_case(raw: dict) -> Case:
    """解析并全量结构校验 CASE.json 的原始字典。"""
    _require_object(raw, "$")
    _check_known_keys(raw, _TOP_FIELDS, "$")
    _require_keys(raw, ["now", "atoms", "old_labels", "new_labels",
                        "mappings", "objects", "roles"], "$")

    case_id = "default"
    if "case_id" in raw:
        case_id = _nonempty_str(raw["case_id"], "$.case_id", "case_id")
    now = _nonnegative_int(raw["now"], "$.now", "now")
    old_version = _nonempty_str(raw.get("old_version", "v1"),
                                "$.old_version", "old_version")
    new_version = _nonempty_str(raw.get("new_version", "v2"),
                                "$.new_version", "new_version")

    atoms = _string_list(raw["atoms"], "$.atoms", "atoms")
    if len(set(atoms)) != len(atoms):
        dup = sorted({a for a in atoms if atoms.count(a) > 1})
        raise ValidationError(E_DUPLICATE, f"原子类别 ID 重复：{dup!r}", "$.atoms")
    atom_index = {aid: i for i, aid in enumerate(atoms)}

    old_labels = _parse_labels(raw["old_labels"], "$.old_labels", atom_index)
    new_labels = _parse_labels(raw["new_labels"], "$.new_labels", atom_index)
    if len(old_labels) > MAX_OLD_LABELS:
        raise ValidationError(E_RANGE,
                              f"旧标签至多 {MAX_OLD_LABELS} 个，实际 {len(old_labels)} 个",
                              "$.old_labels")

    old_label_map = {l.id: l for l in old_labels}
    new_label_map = {l.id: l for l in new_labels}

    mappings = _parse_mappings(raw["mappings"], set(old_label_map),
                               new_label_map, "$.mappings")
    mapping_map = {m.old_label: m for m in mappings}
    objects = _parse_objects(raw["objects"], old_label_map, "$.objects")
    object_map = {o.id: o for o in objects}
    roles = _parse_roles(raw["roles"], atom_index, "$.roles")
    role_map = {r.id: r for r in roles}

    plan = None
    if "plan" in raw and raw["plan"] is not None:
        plan = _parse_plan(raw["plan"], mapping_map, "$.plan")

    return Case(
        case_id=case_id,
        now=now,
        old_version=old_version,
        new_version=new_version,
        atoms=tuple(atoms),
        bit_index=atom_index,
        old_labels=old_labels,
        new_labels=new_labels,
        mappings=mappings,
        objects=objects,
        roles=roles,
        plan=plan,
        old_label_map=old_label_map,
        new_label_map=new_label_map,
        mapping_map=mapping_map,
        object_map=object_map,
        role_map=role_map,
    )


def mask_to_atom_ids(mask: int, case: Case) -> list[str]:
    """把位掩码还原为按 $.atoms 顺序排列的原子类别 ID 列表。"""
    return [aid for aid in case.atoms if mask & (1 << case.bit_index[aid])]
