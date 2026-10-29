from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from constants import KNOWN_PLACEHOLDERS
from i18n_opinion import _  # noqa: E402


PUBLIC_CATALOG = Path(__file__).resolve().parents[1] / "catalog"
IDENTIFIER = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
EDITABLE = {"title", "level", "scopes", "text"}
RULE_FIELDS = EDITABLE | {"id", "version", "slot"}
TEMPLATE_FIELDS = {"family", "variant", "version", "name", "description", "scopes", "rules"}
PROFILE_FIELDS = {"schema_version", "id", "version", "templates", "rules", "sources", "overrides", "custom", "derived_from"}
PROFILE_OPTIONAL_FIELDS = {"loading"}
LOADING_FIELDS = {"core", "signals", "bundles"}
BUNDLE_FIELDS = {"family", "trigger", "exclude", "routes"}


class OpinionVersionError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise OpinionVersionError(message)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    for component in (path, *path.parents):
        if component.is_symlink():
            fail(_("拒绝使用符号链接：{0}").format(component))
    return path


def read_text(path: Path) -> str:
    path = safe_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        fail(_("必须使用普通文件：{0}").format(path))
    return path.read_text(encoding="utf-8")


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        fail(_("JSON 根节点必须是对象：{0}").format(path))
    return value


def fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        fail(_("{0} 字段必须为：{1}").format(label, sorted(expected)))


def string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        fail(_("{0} 必须是非空字符串").format(label))


def identifier(value: Any) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail(_("编号无效：{0!r}").format(value))


def version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        fail(_("版本必须是无前导零的三段数字：{0!r}").format(value))
    return tuple(int(part) for part in value.split("."))


def strings(value: Any, label: str, *, empty: bool = False) -> None:
    if not isinstance(value, list) or (not empty and not value):
        fail(_("{0} 必须是字符串数组").format(label))
    for item in value:
        string(item, label)
    if len(value) != len(set(value)):
        fail(_("{0} 包含重复项").format(label))


def parse_ref(ref: str, kind: str) -> tuple[str, str]:
    if not isinstance(ref, str) or ref.count("@") != 1:
        fail(_("{0} 必须精确指定 @VERSION：{1!r}").format(kind, ref))
    identity, release = ref.split("@")
    version(release)
    parts = identity.split("/")
    if len(parts) != (2 if kind == "templates" else 1):
        fail(_("{0} 引用格式无效：{1}").format(kind, ref))
    for part in parts:
        identifier(part)
    return identity, release


def validate_edit(value: Any) -> None:
    fields(value, EDITABLE, _("规则内容"))
    string(value["title"], "title")
    string(value["text"], "text")
    strings(value["scopes"], "scopes")
    if value["level"] not in ("required", "preferred"):
        fail(_("level 必须为 required 或 preferred"))


def release_ref(value: dict[str, Any], kind: str) -> str:
    identity = value["id"] if kind == "rules" else f"{value['family']}/{value['variant']}"
    return f"{identity}@{value['version']}"


def validate_release(value: Any) -> str:
    if not isinstance(value, dict):
        fail(_("发布内容必须是对象"))
    kind = "rules" if "id" in value else "templates"
    fields(value, RULE_FIELDS if kind == "rules" else TEMPLATE_FIELDS, _("发布内容"))
    version(value["version"])
    if kind == "rules":
        identifier(value["id"])
        identifier(value["slot"])
        validate_edit({key: value[key] for key in EDITABLE})
    else:
        identifier(value["family"])
        identifier(value["variant"])
        string(value["name"], "name")
        string(value["description"], "description")
        strings(value["scopes"], "scopes")
        strings(value["rules"], "rules")
        for ref in value["rules"]:
            parse_ref(ref, "rules")
    return kind


def release_path(root: Path, value: dict[str, Any], kind: str) -> Path:
    identity, release = parse_ref(release_ref(value, kind), kind)
    return root / kind / identity / "versions" / f"{release}.json"


def load_catalog(root: Path) -> dict[str, dict[str, Any]]:
    root = safe_path(root)
    if not root.is_dir():
        fail(_("目录不存在：{0}").format(root))
    catalog: dict[str, dict[str, Any]] = {"templates": {}, "rules": {}}
    for path in sorted(root.rglob("*")):
        safe_path(path)
        if path.suffix != ".json":
            continue
        value = read_object(path)
        kind = validate_release(value)
        if path != release_path(root, value, kind):
            fail(_("发布文件路径与身份不一致：{0}").format(path))
        ref = release_ref(value, kind)
        catalog[kind][ref] = value
    for template in catalog["templates"].values():
        sources = resolve(catalog, [release_ref(template, "templates")], [])
        effective_rules({"sources": sources, "overrides": {}})
    return catalog


def resolve(catalog: dict[str, dict[str, Any]], templates: list[str], rules: list[str]) -> dict[str, Any]:
    families: set[str] = set()
    sources: dict[str, list[Any]] = {"templates": [], "rules": []}
    references = list(rules)
    for ref in templates:
        identity, _version = parse_ref(ref, "templates")
        family = identity.split("/")[0]
        if family in families:
            fail(_("同一模板系列只能选择一个变体和版本：{0}").format(family))
        families.add(family)
        if ref not in catalog["templates"]:
            fail(_("未知模板：{0}").format(ref))
        value = catalog["templates"][ref]
        sources["templates"].append({"ref": ref, "sha256": digest(value), "value": value})
        references.extend(value["rules"])
    selected: dict[str, Any] = {}
    for ref in references:
        identity, _version = parse_ref(ref, "rules")
        if ref not in catalog["rules"]:
            fail(_("未知规则：{0}").format(ref))
        value = catalog["rules"][ref]
        if identity in selected and selected[identity]["ref"] != ref:
            fail(_("规则选择了不同版本：{0}").format(identity))
        selected.setdefault(identity, {"ref": ref, "sha256": digest(value), "value": value})
    sources["rules"] = list(selected.values())
    return sources


def base_rules(profile: dict[str, Any]) -> dict[str, Any]:
    return {source["value"]["id"]: source["value"] for source in profile["sources"]["rules"]}


def effective_rules(profile: dict[str, Any]) -> dict[str, Any]:
    result = dict(base_rules(profile))
    for identity, override in profile["overrides"].items():
        if override is None:
            result.pop(identity, None)
        else:
            result[identity] = override
    slots: dict[str, str] = {}
    for identity, rule in result.items():
        slot = rule["slot"]
        if slot in slots:
            fail(_("规则 slot 冲突：{0}（{1}、{2}）").format(slot, slots[slot], identity))
        slots[slot] = identity
    return result


def selected_families(profile: dict[str, Any]) -> list[str]:
    return [parse_ref(ref, "templates")[0].split("/")[0] for ref in profile["templates"]]


def validate_loading(profile: dict[str, Any]) -> None:
    loading = profile["loading"]
    fields(loading, LOADING_FIELDS, "loading")
    strings(loading["core"], "loading.core", empty=True)
    if not isinstance(loading["signals"], dict) or not loading["signals"]:
        fail(_("loading.signals 必须是非空对象"))
    for key, values in loading["signals"].items():
        identifier(key)
        strings(values, f"loading.signals.{key}")
        for item in values:
            identifier(item)
    if not isinstance(loading["bundles"], list) or not loading["bundles"]:
        fail(_("loading.bundles 至少包含一个按需规则束"))
    families = set(selected_families(profile))
    seen = set(loading["core"])
    for family in loading["core"]:
        if family not in families:
            fail(_("loading.core 引用了未选择的模板系列：{0}").format(family))
    for bundle in loading["bundles"]:
        fields(bundle, BUNDLE_FIELDS, _("loading.bundles 项"))
        family = bundle["family"]
        if family not in families:
            fail(_("loading.bundles 引用了未选择的模板系列：{0!r}").format(family))
        if family in seen:
            fail(_("模板系列在 loading 中重复出现：{0}").format(family))
        seen.add(family)
        string(bundle["trigger"], "trigger")
        string(bundle["exclude"], "exclude")
        if not isinstance(bundle["routes"], list) or not bundle["routes"]:
            fail(_("规则束至少需要一条路由：{0}").format(family))
        for route in bundle["routes"]:
            if not isinstance(route, dict) or not route:
                fail(_("路由必须是非空对象：{0}").format(family))
            for key, values in route.items():
                strings(values, f"routes.{key}")
                if key not in loading["signals"] or not set(values) <= set(loading["signals"][key]):
                    fail(_("路由使用了未声明的信号：{0}.{1}").format(family, key))


def validate_profile(value: Any) -> dict[str, Any]:
    tiered = isinstance(value, dict) and value.get("schema_version") == "2.1"
    fields(value, PROFILE_FIELDS | PROFILE_OPTIONAL_FIELDS if tiered else PROFILE_FIELDS, "Profile")
    if value["schema_version"] not in ("2.0", "2.1"):
        fail(_("Profile schema_version 必须为 2.0 或 2.1"))
    identifier(value["id"])
    version(value["version"])
    strings(value["templates"], "templates", empty=True)
    strings(value["rules"], "rules", empty=True)
    fields(value["sources"], {"templates", "rules"}, "sources")
    catalog: dict[str, dict[str, Any]] = {"templates": {}, "rules": {}}
    for kind, sources in value["sources"].items():
        if not isinstance(sources, list):
            fail(_("sources 项必须是数组"))
        for source in sources:
            fields(source, {"ref", "sha256", "value"}, "source")
            if validate_release(source["value"]) != kind:
                fail(_("source 类型不一致"))
            if source["ref"] != release_ref(source["value"], kind) or source["sha256"] != digest(source["value"]):
                fail(_("source 指纹或引用不一致：{0}").format(source['ref']))
            if source["ref"] in catalog[kind]:
                fail(_("重复 source：{0}").format(source['ref']))
            catalog[kind][source["ref"]] = source["value"]
    if resolve(catalog, value["templates"], value["rules"]) != value["sources"]:
        fail(_("Profile 引用与 sources 不一致"))
    if not isinstance(value["overrides"], dict):
        fail(_("overrides 必须是对象"))
    for identity, override in value["overrides"].items():
        identifier(identity)
        if override is not None:
            if validate_release(override) != "rules" or override["id"] != identity:
                fail(_("override 必须使用匹配编号的完整规则"))
        elif identity not in base_rules(value):
            fail(_("停用规则不存在：{0}").format(identity))
    if value["custom"] is not None:
        string(value["custom"], "custom")
    if value["derived_from"] is not None:
        fields(value["derived_from"], {"id", "version", "sha256"}, "derived_from")
        identifier(value["derived_from"]["id"])
        version(value["derived_from"]["version"])
        if not isinstance(value["derived_from"]["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["derived_from"]["sha256"]):
            fail(_("derived_from 指纹无效"))
    if not effective_rules(value) and not value["custom"]:
        fail(_("Profile 至少需要一条有效规则或自定义内容"))
    if tiered:
        validate_loading(value)
    return value


def load_profile(path: Path) -> dict[str, Any]:
    return validate_profile(read_object(path))


def replace_refs(previous: list[str], incoming: list[str], kind: str) -> list[str]:
    result = list(previous)
    keys: set[str] = set()
    for ref in incoming:
        identity, _version = parse_ref(ref, kind)
        key = identity.split("/")[0] if kind == "templates" else identity
        if key in keys:
            fail(_("同一次选择包含重复身份：{0}").format(key))
        keys.add(key)
        result = [item for item in result if (parse_ref(item, kind)[0].split("/")[0] if kind == "templates" else parse_ref(item, kind)[0]) != key]
        result.append(ref)
    return result


def behavior(rule: dict[str, Any] | None) -> Any:
    return None if rule is None else {key: rule[key] for key in RULE_FIELDS - {"version", "title"}}


def comparison(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    before, after = effective_rules(old), effective_rules(new)
    base, upstream = base_rules(old), base_rules(new)
    conflicts = []
    for identity in sorted(set(base) | set(upstream)):
        if identity in old["overrides"] and base.get(identity) != upstream.get(identity):
            local = before.get(identity)
            if behavior(local) != behavior(upstream.get(identity)) and behavior(base.get(identity)) != behavior(upstream.get(identity)):
                conflicts.append({"id": identity, "base": base.get(identity), "local": local, "upstream": upstream.get(identity)})
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": [identity for identity in sorted(set(before) & set(after)) if before[identity] != after[identity]],
        "custom_changed": old["custom"] != new["custom"],
        "templates": {"before": old["templates"], "after": new["templates"]},
        "direct_rules": {"before": old["rules"], "after": new["rules"]},
        "rule_changes": [{"id": identity, "before": before.get(identity), "after": after.get(identity)} for identity in sorted(set(before) | set(after)) if before.get(identity) != after.get(identity)],
        "custom": {"before": old["custom"], "after": new["custom"]},
        "loading_changed": old.get("loading") != new.get("loading"),
        "loading": {"before": old.get("loading"), "after": new.get("loading")},
        "conflicts": conflicts,
    }


def immutable_write(path: Path, value: dict[str, Any]) -> str:
    path = safe_path(path)
    if path.exists():
        if read_object(path) == value:
            return "unchanged"
        fail(_("已经发布的版本不可修改：{0}").format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(pretty(value))
            handle.flush()
            os.fsync(handle.fileno())
        # 硬链接只创建不存在的目标，避免并发发布覆盖已经存在的版本。
        os.link(temporary, path)
    finally:
        temporary.unlink()
    if read_object(path) != value:
        fail(_("写入后核对失败：{0}").format(path))
    return "written"


def confirm(args: argparse.Namespace, fingerprint: str) -> None:
    if args.confirm != fingerprint:
        fail(_("请先审阅预览，再使用 --confirm {0} --apply").format(fingerprint))


def profile_command(args: argparse.Namespace) -> dict[str, Any]:
    root = safe_path(args.project)
    if not root.is_dir():
        fail(_("项目目录不存在：{0}").format(root))
    identifier(args.id)
    version(args.version)
    old = load_profile(args.from_profile) if args.from_profile else None
    if old and args.id == old["id"] and version(args.version) <= version(old["version"]):
        fail(_("修订 Profile 必须增加版本；私有派生必须使用独立编号"))
    templates = replace_refs(old["templates"] if old else [], args.template, "templates")
    rules = replace_refs(old["rules"] if old else [], args.rule, "rules")
    for family in args.remove_template:
        identifier(family)
        if not any(parse_ref(ref, "templates")[0].split("/")[0] == family for ref in templates):
            fail(_("无法移除未选择的模板系列：{0}").format(family))
        templates = [ref for ref in templates if parse_ref(ref, "templates")[0].split("/")[0] != family]
    for identity in args.remove_rule:
        identifier(identity)
        if not any(parse_ref(ref, "rules")[0] == identity for ref in rules):
            fail(_("无法移除未直接选择的规则：{0}").format(identity))
        rules = [ref for ref in rules if parse_ref(ref, "rules")[0] != identity]
    catalog = load_catalog(args.catalog)
    if old:
        for kind, sources in old["sources"].items():
            for source in sources:
                existing = catalog[kind].get(source["ref"])
                if existing is not None and digest(existing) != source["sha256"]:
                    fail(_("已经发布的来源内容发生变化：{0}").format(source['ref']))
                catalog[kind].setdefault(source["ref"], source["value"])
    custom = read_text(args.custom_file).strip() if args.custom_file else (old["custom"] if old else None)
    if args.loading_file and args.remove_loading:
        fail(_("--loading-file 与 --remove-loading 不能同时使用"))
    # 加载声明整体替换；未提供时沿用原 Profile，保证未变更的声明不会在修订中丢失。
    loading = read_object(args.loading_file) if args.loading_file else (None if args.remove_loading or not old else old.get("loading"))
    value = {
        "schema_version": "2.0" if loading is None else "2.1", "id": args.id, "version": args.version,
        "templates": templates, "rules": rules, "sources": resolve(catalog, templates, rules),
        "overrides": dict(old["overrides"]) if old else {}, "custom": custom,
        "derived_from": {"id": old["id"], "version": old["version"], "sha256": digest(old)} if old else None,
    }
    if loading is not None:
        value["loading"] = loading
    changes = comparison(old, value) if old else {"conflicts": []}
    resolutions = read_object(args.resolutions_file) if args.resolutions_file else {}
    conflict_ids = {item["id"] for item in changes["conflicts"]}
    if set(resolutions) - conflict_ids:
        fail(_("resolutions 包含没有冲突的编号"))
    for identity, choice in resolutions.items():
        if choice == "use-upstream":
            value["overrides"].pop(identity, None)
        elif choice != "keep-local":
            fail(_("冲突处理必须为 keep-local 或 use-upstream"))
    overrides = read_object(args.overrides_file) if args.overrides_file else {}
    for identity, override in overrides.items():
        identifier(identity)
        if identity not in base_rules(value):
            fail(_("override 引用未知规则：{0}").format(identity))
        if override is None:
            value["overrides"][identity] = None
        else:
            validate_edit(override)
            value["overrides"][identity] = {**base_rules(value)[identity], **override}
    unresolved = conflict_ids - set(resolutions) - set(overrides)
    # 上游删除的停用记录没有实际作用，可以直接移除。
    for identity in list(value["overrides"]):
        if value["overrides"][identity] is None and identity not in base_rules(value):
            value["overrides"].pop(identity)
    validate_profile(value)
    if old:
        updated_changes = comparison(old, value)
        updated_changes["conflicts"] = changes["conflicts"]
        changes = updated_changes
    path = root / ".opinion" / "profiles" / args.id / "versions" / f"{args.version}.json"
    fingerprint = digest(value)
    result = {"ok": True, "status": "preview", "path": str(path), "profile": value, "content": render(value), "effective_rules": list(effective_rules(value).values()), "changes": changes, "unresolved": sorted(unresolved), "confirmation_sha256": fingerprint}
    if args.apply:
        if unresolved:
            fail(_("升级存在未确认的冲突：{0}").format(sorted(unresolved)))
        confirm(args, fingerprint)
        result["status"] = immutable_write(path, value)
    return result


def publish_command(args: argparse.Namespace) -> dict[str, Any]:
    value = read_object(args.file)
    kind = validate_release(value)
    catalog = load_catalog(args.catalog)
    ref = release_ref(value, kind)
    existing = catalog[kind].get(ref)
    if existing is not None and existing != value:
        fail(_("已经发布的版本不可修改：{0}").format(ref))
    identity, release = parse_ref(ref, kind)
    patch_peers = [item for key, item in catalog[kind].items() if parse_ref(key, kind)[0] == identity and version(item["version"])[:2] == version(release)[:2] and item["version"] != release]
    for latest in patch_peers:
        before, after = version(latest["version"]), version(release)
        if before[:2] == after[:2]:
            if kind == "rules" and behavior(latest) != behavior(value):
                fail(_("patch 版本只允许不改变行为的修改"))
            if kind == "templates":
                if latest["scopes"] != value["scopes"]:
                    fail(_("patch 版本不能改变适用范围"))
                a = resolve(catalog, [release_ref(latest, kind)], [])
                prospective = {**catalog, "templates": {**catalog["templates"], ref: value}}
                b = resolve(prospective, [ref], [])
                if {s["value"]["id"]: behavior(s["value"]) for s in a["rules"]} != {s["value"]["id"]: behavior(s["value"]) for s in b["rules"]}:
                    fail(_("patch 版本不能改变有效规则"))
    catalog[kind][ref] = value
    if kind == "templates":
        sources = resolve(catalog, [ref], [])
        effective_rules({"sources": sources, "overrides": {}})
    root = safe_path(args.catalog)
    path = release_path(root, value, kind)
    fingerprint = digest(value)
    result = {"ok": True, "status": "preview", "kind": kind, "ref": ref, "path": str(path), "release": value, "confirmation_sha256": fingerprint}
    if args.apply:
        confirm(args, fingerprint)
        if (root == PUBLIC_CATALOG or PUBLIC_CATALOG in root.parents) and not args.public_approved:
            fail(_("公开内容需要用户批准完整文件，并使用 --public-approved"))
        result["status"] = immutable_write(path, value)
    return result


def render_rule(profile: dict[str, Any], rule: dict[str, Any]) -> str:
    level = "强制规则" if rule["level"] == "required" else "偏好"
    origin = f"个人修订：`{profile['id']}@{profile['version']}`" if rule["id"] in profile["overrides"] else f"规则：`{rule['id']}@{rule['version']}`"
    return "\n".join((f"## {rule['title']}", "", f"{origin}；{level}；适用：{'、'.join(rule['scopes'])}。", "", rule["text"]))


def render(profile: dict[str, Any]) -> str:
    metadata = {"schema_version": "2.0", "profile": f"{profile['id']}@{profile['version']}", "sha256": digest(profile), "templates": profile["templates"], "rules": profile["rules"]}
    lines = ["<!-- opinion-manager:metadata", pretty(metadata).rstrip(), "-->", "", "# Agent Opinion", "", "本文件包含当前环境中用户确认的 Agent 行为、表达、判断与交付要求。"]
    for rule in effective_rules(profile).values():
        lines.extend(("", render_rule(profile, rule)))
    if profile["custom"]:
        lines.extend(("", "## 用户自定义规则", "", profile["custom"]))
    return "\n".join(lines).rstrip() + "\n"


def file_state(path: Path) -> str | None:
    safe_path(path)
    return read_text(path) if path.exists() else None


def lock_value(profile: dict[str, Any], content: str) -> dict[str, Any]:
    return {"schema_version": "2.0", "renderer_version": "2.0", "profile": profile, "profile_sha256": digest(profile), "opinion_sha256": text_digest(content)}


def validate_lock(lock: Any) -> dict[str, Any]:
    fields(lock, {"schema_version", "renderer_version", "profile", "profile_sha256", "opinion_sha256"}, "lock")
    if lock["schema_version"] != "2.0" or lock["renderer_version"] != "2.0":
        fail(_("锁定文件版本不受支持"))
    profile = validate_profile(lock["profile"])
    if lock != lock_value(profile, render(profile)):
        fail(_("锁定文件指纹与内容不一致"))
    return lock


def atomic_write(path: Path, content: str) -> None:
    safe_path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def compose_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.template or args.rule or args.custom_file:
        fail(_("--profile 必须独立使用；通过 profile 命令修改选择和自定义内容"))
    profile = load_profile(args.profile)
    root = safe_path(args.project)
    if not root.is_dir():
        fail(_("项目目录不存在：{0}").format(root))
    content = render(profile)
    lock = lock_value(profile, content)
    destination, lock_path = root / "OPINION.md", root / "opinion.lock.json"
    current, current_lock = file_state(destination), file_state(lock_path)
    blocked = None
    if current_lock is not None:
        previous = validate_lock(json.loads(current_lock))
        previous_profile = previous["profile"]
        if (previous_profile["id"], previous_profile["version"]) == (profile["id"], profile["version"]) and digest(previous_profile) != digest(profile):
            fail(_("已经锁定的 Profile 版本不可改变内容；请发布新的 Profile 版本"))
        if current is None:
            if previous != lock:
                blocked = _("正文缺失；请先使用原锁定 Profile 重建正文，再进行升级")
        elif text_digest(current) != previous["opinion_sha256"]:
            if args.accept_current != text_digest(current):
                blocked = _("OPINION.md 已被人工修改；请读取并纳入新的 Profile，审阅后用 --accept-current 当前文件SHA256 确认")
    elif current is not None and current != content and not args.replace:
        # 完整匹配初始化占位内容，任何追加内容均需要审阅。
        if current not in KNOWN_PLACEHOLDERS:
            blocked = _("已有 OPINION.md 需要审阅并使用 --replace")
    fingerprint = digest({"lock": lock, "before": {"opinion": current, "lock": current_lock}, "replace": args.replace, "accept_current": args.accept_current})
    result = {"ok": True, "status": "preview", "content": content, "lock": lock, "blocked": blocked, "confirmation_sha256": fingerprint, "path": str(destination)}
    if args.apply:
        if blocked:
            fail(blocked)
        confirm(args, fingerprint)
        if current == content and current_lock is not None and json.loads(current_lock) == lock:
            result["status"] = "unchanged"
        else:
            # 两个文件分别原子写入；中断后的不一致由 verify 明确报告。
            atomic_write(destination, content)
            atomic_write(lock_path, pretty(lock))
            verify_project(root)
            result["status"] = "written"
    return result


def verify_project(project: Path) -> dict[str, Any]:
    root = safe_path(project)
    lock = validate_lock(read_object(root / "opinion.lock.json"))
    if text_digest(read_text(root / "OPINION.md")) != lock["opinion_sha256"]:
        fail(_("OPINION.md 与锁定文件不一致；请读取人工修改或检查中断的写入"))
    return {"ok": True, "status": "verified", "profile": f"{lock['profile']['id']}@{lock['profile']['version']}", "profile_sha256": lock["profile_sha256"], "opinion_sha256": lock["opinion_sha256"]}


def updates_command(args: argparse.Namespace) -> dict[str, Any]:
    profile = load_profile(args.profile)
    catalog = load_catalog(args.catalog)
    for kind, sources in profile["sources"].items():
        for source in sources:
            item = catalog[kind].get(source["ref"])
            if item is not None and digest(item) != source["sha256"]:
                fail(_("已经发布的来源内容发生变化：{0}").format(source['ref']))
    updates = []
    for kind in ("templates", "rules"):
        for ref in profile[kind]:
            identity, current = parse_ref(ref, kind)
            candidates = [candidate for candidate in catalog[kind] if parse_ref(candidate, kind)[0] == identity and version(parse_ref(candidate, kind)[1]) > version(current)]
            if candidates:
                updates.append({"kind": kind, "current": ref, "available": sorted(candidates, key=lambda candidate: version(parse_ref(candidate, kind)[1]))})
    return {"ok": True, "updates": updates, "applied": False}


def options(parser: argparse.ArgumentParser, *, catalog: bool = False, mutation: bool = False) -> None:
    parser.add_argument("--output", choices=("json", "markdown"), default="json")
    if catalog:
        parser.add_argument("--catalog", type=Path, default=PUBLIC_CATALOG)
    if mutation:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm", help=_("预览返回的完整内容 SHA-256"))


def add_commands(subparsers: Any, compose: argparse.ArgumentParser, catalog: argparse.ArgumentParser) -> None:
    compose.add_argument("--profile", type=Path)
    compose.add_argument("--confirm")
    compose.add_argument("--accept-current", help=_("人工修改或写入中断后的当前 OPINION.md SHA-256；读取并保留内容后使用"))
    catalog.add_argument("--catalog", type=Path)
    profile = subparsers.add_parser("profile", help=_("预览、派生和发布不可变的个人 Profile"))
    options(profile, catalog=True, mutation=True)
    profile.add_argument("--project", required=True, type=Path)
    profile.add_argument("--id", required=True)
    profile.add_argument("--version", required=True)
    profile.add_argument("--template", action="append", default=[])
    profile.add_argument("--rule", action="append", default=[])
    profile.add_argument("--remove-template", action="append", default=[])
    profile.add_argument("--remove-rule", action="append", default=[])
    profile.add_argument("--custom-file", type=Path)
    profile.add_argument("--from-profile", type=Path)
    profile.add_argument("--overrides-file", type=Path)
    profile.add_argument("--resolutions-file", type=Path)
    profile.add_argument("--loading-file", type=Path, help=_("完整的分层加载声明 JSON；整体替换原声明"))
    profile.add_argument("--remove-loading", action="store_true", help=_("移除分层加载声明，恢复为完整读取"))
    publish = subparsers.add_parser("publish", help=_("预览或发布规则、模板版本"))
    options(publish, catalog=True, mutation=True)
    publish.add_argument("--file", required=True, type=Path)
    publish.add_argument("--public-approved", action="store_true")
    updates = subparsers.add_parser("updates", help=_("只读检查同一变体的新版本"))
    options(updates, catalog=True)
    updates.add_argument("--profile", required=True, type=Path)
    compare = subparsers.add_parser("compare", help=_("查看两份 Profile 的差异和三方冲突"))
    options(compare)
    compare.add_argument("--from-profile", required=True, type=Path)
    compare.add_argument("--to-profile", required=True, type=Path)
    verify = subparsers.add_parser("verify", help=_("核对正文、Profile 快照和锁定指纹"))
    options(verify)
    verify.add_argument("--project", required=True, type=Path)


def handles(args: argparse.Namespace) -> bool:
    return args.command in {"profile", "publish", "updates", "compare", "verify"} or (args.command == "compose" and args.profile is not None) or (args.command == "catalog" and args.catalog is not None)


def execute(args: argparse.Namespace) -> tuple[int, str]:
    if args.command == "profile":
        result = profile_command(args)
    elif args.command == "publish":
        result = publish_command(args)
    elif args.command == "compose":
        result = compose_command(args)
    elif args.command == "verify":
        result = verify_project(args.project)
    elif args.command == "updates":
        result = updates_command(args)
    elif args.command == "compare":
        result = {"ok": True, **comparison(load_profile(args.from_profile), load_profile(args.to_profile))}
    else:
        catalog = load_catalog(args.catalog)
        result = {"ok": True, "templates": list(catalog["templates"].values()), "rules": list(catalog["rules"].values())}
    if args.output == "markdown":
        if args.command == "compose":
            return 0, result["content"]
        return 0, "```json\n" + pretty(result) + "```\n"
    return 0, pretty(result)
