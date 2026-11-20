# Opinion 变体、版本与个人 Profile

## 身份与选择

- 模板系列 `family` 标识用途，例如 `work-email`。
- 模板变体 `variant` 标识同一用途下的平行偏好，例如 `formal-report`。各个变体维护独立版本。
- 模板引用为 `family/variant@VERSION`，规则引用为 `ID@VERSION`。版本使用无前导零的 `major.minor.patch`。省略版本、使用 `latest` 和版本范围都会被拒绝。
- 同一个系列在一份 Profile 中只能选择一个变体和版本。不同系列可以组合。
- 原子规则包含 `id`、`version`、`slot`、`title`、`level`、`scopes`、`text`。`level` 为 `required` 或 `preferred`。
- `slot` 表示一项行为选择。同一个有效 slot 出现两条规则会终止生成；相同规则和版本会去重，相同规则的不同版本会报告冲突。用于不同场景的独立选择应分配不同 slot。
- 模板包含 `family`、`variant`、`version`、`name`、`description`、`scopes` 和精确规则引用数组 `rules`。模板名称与说明用于帮助用户选择。

本文中的示例名称说明数据格式；实际可用规则、系列、变体与版本以 `catalog` 输出为准。随包候选目录说明见 `../catalog/README.md`，全部模板均为可选择的偏好。

## 文件与版本

每个规则和模板版本分别保存在规则目录：

- `rules/ID/versions/VERSION.json`
- `templates/FAMILY/VARIANT/versions/VERSION.json`

每个项目的个人 Profile 保存在 `.opinion/profiles/ID/versions/VERSION.json`。Profile 包含精确模板引用、直接选择的规则、完整来源快照、个人 overrides、自定义正文及派生来源指纹。旧版本保留。项目根目录保存完整的 `OPINION.md` 和 `opinion.lock.json`。

锁定文件包含完整 Profile 快照、Profile 指纹、正文指纹和渲染格式版本。运行 Agent 时读取完整正文；Profile 声明了分层加载时，可以按「分层加载」一节读取核心规则与规则束，两种方式得到的规则文字逐字一致。重建和核验不需要访问模板仓库或其他项目的本机路径。相同 Profile 在相同渲染格式下生成相同正文，正文没有变化的生成时间。

指纹使用 SHA-256：JSON 使用 UTF-8、按字段名称排序、无额外空格的规范序列化；正文使用原始 UTF-8 内容。调整 JSON 排版不会改变内容指纹。

`publish` 和 `profile` 只创建新版本，重复发布完全相同内容返回 `unchanged`；相同身份与版本的不同内容被拒绝。工具不会修改已发布文件。使用中发现已锁定来源被人工修改时，升级检查和修订都会终止。

版本含义：

- patch：拼写、名称或说明等不改变行为的修订。工具检查规则正文、等级、适用范围和 slot；模板检查有效规则与适用范围。
- minor：符合既有目标的行为完善或新增规则；发布者必须审查兼容性，用户确认后才能采用。
- major：改变既有目标、删除约束或引入不兼容行为；发布者必须说明适用条件并重新验证。

任何版本更新都需要预览与确认。工具可以校验结构和依赖；行为兼容性需要内容审查。

## 通用命令约定

以下命令在完整分发仓库根目录执行，要求 Python 3.11 及以上。独立安装插件时，将示例中的 `plugins/opinion-manager/runtime/opinion_manager.py` 替换为该插件实际目录下的 `runtime/opinion_manager.py`，将 `plugins/opinion-manager/catalog` 替换为该插件的 `catalog` 目录。`/path/to/project` 必须已经存在。使用用户明确指定的私有目录保存个人内容，公开仓库中不得保存个人 Profile、来源文件或私有规则目录。

所有写入默认预览。预览 JSON 包含 `confirmation_sha256`；向用户展示完整内容并取得确认后，在相同命令后追加 `--apply --confirm SHA256`。用户批准的是规则内容，Agent 负责传递指纹。任何相关内容在批准后改变，都必须重新预览。

查看公开规则目录：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py catalog \
  --catalog plugins/opinion-manager/catalog --output json
```

目录为空时，使用自定义生成或跳过配置。

## 自定义生成

读取用户已有说明，整理后展示完整规则。用户确认后使用文件编辑工具保存到用户指定的私有文件。已有规则文件可以直接使用，无需重复复制。

预览个人 Profile：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py profile \
  --project /path/to/project --id private.my-opinion --version 1.0.0 \
  --custom-file /path/to/confirmed-rules.md
```

将 Profile 预览的 profile 对象保存为项目私有候选 JSON，正式 Profile 此时尚未保存。先以候选路径预览完整正文与锁，再集中确认内容：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py compose \
  --project /path/to/project \
  --profile /path/to/private-preview/profile.json \
  --output json
```

两份预览内容确认后，写入前重新核对两项指纹。先用 Profile 指纹保存正式 Profile，再把 compose 路径换成正式 Profile，用 compose 指纹保存正文与锁；完整步骤见 [首次使用说明](onboarding.md)。原有正文包含实际规则时，先读取并纳入 Profile，使用 `--replace` 重新预览。初始化器原样生成的空白占位内容可以直接替换。

最后核验：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py verify --project /path/to/project
```

后续自定义内容修订使用 `profile --from-profile <原文件> --id <相同编号> --version <新版本> --custom-file <完整的新自定义正文>`。`--custom-file` 替换自定义段落；新文件必须包含仍需保留的全部自定义要求。

## 简单模式与复杂模式

已有批准的版本化模板时，`profile` 使用 `--catalog <目录>`，并重复传入 `--template family/variant@VERSION`。预览每个系列的变体与说明，用户选择后展示最终合并内容。

复杂模式可以重复传入 `--rule ID@VERSION` 逐条选择。也可以选择完整模板，然后使用 `--overrides-file <JSON文件>` 停用其中未确认的条目。问答由 Agent 交互完成，未确认的推荐不进入 Profile。

overrides 是规则编号到内容的对象：值为 `null` 表示停用；值为对象时必须完整包含 `title`、`level`、`scopes`、`text`。只能修改当前来源中的规则，不能修改其编号和 slot。新增个人原子规则应在私有目录独立发布后通过 `--rule` 引入；个人自定义段落也可以表达新的要求。

多个模板发布版本都保留时，`catalog` 展示全部版本和变体；工具不会替用户选择最新版本。

## 首次配置的选择与确认

首次只提供直接用模板、选择部分条目、从空白开始三个主入口，另可基于已授权记忆提出候选。使用 [首次使用说明](onboarding.md) 的流程，避免提前展开偏好问卷。

Profile 和 compose 可在正式保存前各自预览。用户一次确认完整正文、来源、个人修改、差异与拟保存内容；Agent 分别使用两项实际返回指纹。写入前重新预览核对，内容变化重新审阅；未解决冲突和 blocked 状态不得写入。两次写入独立保护，不保证系统事务。

默认模板只作为预览建议，确认前不生效。用户选择部分条目时保留精确模板引用并通过 overrides 停用；直接原子规则的 Profile 仅追踪原子规则更新。每个系列只选一个变体，slot 冲突须集中裁决。

Agent 为适用范围与候选含义做判断；scopes 只提供元数据。工具不替 Agent 判断任务属于哪个场景，不检索记忆，也不在后台自动晋升；候选与使用次数可以用「候选、灰度与使用记录」一节的命令记账，裁决始终来自用户；Profile 声明分层加载后，工具只按用户确认的声明切分正文，并核对 Agent 声明的交付信号。新观察只有用户确认后才能形成新规则版本。用户从空白开始时不建立空版本，已有规则保持原样。

## 分层加载

规则较多时，可以在 Profile 中声明哪些模板系列始终读取、哪些在任务命中时再读取。声明是 Profile 的一部分，与规则一样经过完整预览、用户确认和指纹锁定；没有声明的 Profile 保持完整读取，行为与此前版本相同。

声明文件是一个 JSON 对象，通过 `profile --loading-file <文件>` 整体写入：

```json
{
  "core": ["baseline"],
  "signals": {
    "artifact": ["report", "email", "other"],
    "activity": ["rule-maintenance", "none"]
  },
  "bundles": [
    {
      "family": "work-email",
      "trigger": "撰写或审查对外邮件",
      "exclude": "只是在回复中引用邮件内容",
      "routes": [{"artifact": ["email"]}]
    }
  ]
}
```

- `core` 列出始终读取的模板系列。直接选择的原子规则、用户自定义段，以及没有出现在声明中的模板系列，同样始终读取；新增模板不会因为漏登记而被跳过。
- `bundles` 列出按需读取的模板系列。`trigger` 与 `exclude` 是给 Agent 的适用与不适用说明，会原样出现在索引中。
- `signals` 是交付信号词表，`routes` 把信号映射到规则束：一条路由内的所有键都命中时该路由成立，任一路由成立即要求读取该规则束。
- 一条规则同时属于常驻系列和按需系列时归入常驻。带声明的 Profile 使用 `schema_version` 2.1，旧版本工具会拒绝读取并给出明确错误；不带声明的 Profile 仍为 2.0，指纹不变。

修订时未提供 `--loading-file` 则沿用原声明；`--remove-loading` 移除声明并恢复完整读取。移除了声明中引用的模板系列时，必须同时提供新的声明文件。

### 标签选择（schema 2.2）

规则较多且横跨多个场景时，可以改用标签声明：一条规则带多个标签，按需读取标签的并集。设计与决定见 `docs/design/opinion-progressive-loading.md`。声明同样用 `profile --loading-file` 整体写入，`loading` 只含三项，与上面的 `core` / `signals` / `bundles` 互斥：

```json
{
  "always": ["om.chinese-response"],
  "tags": [{"id": "decisions", "name": {"zh": "方案与论证", "en": "Options"},
            "trigger": {"zh": "给出方案或比较时", "en": "When proposing or comparing options"},
            "exclude": {"zh": "纯执行", "en": "Pure execution"}}],
  "members": {"om.calculations": ["decisions"], "om.complete-options": ["decisions"]}
}
```

- 标签词表封闭，标签 id 与规则编号只能是 ASCII（标签 id 为小写字母、数字、连字符，不超过 32 字符，不能取 `core`、`none`、`all`）；中文只写在 `name`、`trigger`、`exclude` 里，且必须同时给出 zh 与 en。
- `always` 列出常驻规则；没有任何标签的规则同样常驻；规则同时在 `always` 与标签里时归常驻。悬空规则编号、未知标签、重复项、没有成员的标签都在预览阶段被拒绝。
- `context core` 输出常驻规则与标签索引；`context select --tag A --tag B` 输出标签并集的规则（不含常驻规则，去重）；`context index` 给出逐标签的规则数与字节数；`context check --tag A [--loaded A]` 由声明的标签算出应读取的规则并核对已读取，缺失退出码 1。未知标签、空选择、全部常驻的标签都会报错并列出有效标签。2.2 声明下 `context bundle` 报错并指向 `select`。
- 自定义段的规则可以在行尾带 ` [tags a b]`（纯 ASCII、字母序；与路由并存时写作 `[route …] [tags …]`），所有标签都在词表内的行按标签按需读取；没有标记、手写、认不出或带词表外标签的行永远常驻。`inbox add` 与 `inbox revise` 用 `--tag`（可重复）指定标签并对照词表校验，`revise --tag-clear` 清空。
- 回退与 2.1 一致：核对失败、没有声明或 `opinion.loading_mode` 为 `full` 时，`core`、`select`、`index`、`check` 都返回 `fallback`，改为完整读取 `OPINION.md`。使用记账见「候选、灰度与使用记录」的 `loaded_tags` 与 `routing_miss_tags`。
- 带标签声明的 Profile 使用 `schema_version` 2.2。旧版本工具读它会报字段集合不符并回退为完整读取，`OPINION.md` 仍是完整正文；2.0、2.1 的 Profile、正文与锁字节不变。

### 模板声明与预览物化（catalog 1.1.0）

公开模板可以带可选的 `loading`，形态与上面的 2.2 声明相同，只能引用本模板的规则编号；它参与模板指纹，只能随模板新版本发布（patch 版本不能改变它）。`scenario/reviewed-project` 与 `scenario/concise-project` 的 1.1.0 声明了常驻集合和四个标签（`decisions`、`files`、`reports`、`maintenance`）；其余模板没有声明，物化后规则全部常驻。

- `profile` 预览选择带声明的模板时，自动生成 Profile 的 `loading`（schema 2.2），在 `changes.loading_changed` 与 `changes.loading.before/after` 中展示，预览输出的 `loading_source` 标明来源（`template`、`inherited`、`file`、`removed`、`none`）。沿用「预览 → 确认指纹 → 写入」，确认前不生效。
- 只在新建 Profile 或模板选择发生变化（例如从 1.0.0 升到 1.1.0）时物化；模板不变的修订沿用原声明。原声明恰好等于旧模板物化的结果时，随模板更新；原声明是用户手写的，不自动合并，只在预览的 `loading_suggestion` 里给出建议，需要时用 `--loading-file` 整体写入。
- 多个模板同时声明时：标签词表按 id 合并，同一 id 的定义必须逐字相同，否则预览报「标签定义冲突」并列出来源；`always` 与 `members` 取并集；没有声明的模板，其规则全部常驻；规则同时在 `always` 与标签里时归常驻。直接选择的原子规则、`overrides` 停用或修订的规则，按规则编号沿用成员关系；停用后标签没有有效规则时，读取侧按「空」处理。
- `--no-tags` 不物化模板声明，保持完整读取（schema 2.0）；`--loading-file`、`--remove-loading` 的行为不变，二者与 `--no-tags` 不能同时用于整体写入。已经选择了带声明模板、又用 `--no-tags` 写入的 Profile，在下次改变模板选择时会再次得到物化预览，仍可再加 `--no-tags`。
- `updates` 对每个可升级的模板列出各版本的声明摘要（`loading`，没有声明为 `null`）；升级预览走 `profile --template <新版本>`，`compose` 仍会因人工修改阻断，需读取并纳入后用 `--accept-current` 确认。
- `untranslated` 的范围包含标签的 `name`、`trigger`、`exclude`；缺译文只警告，不阻断。

读取与核对使用只读的 `context` 命令：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py context core --project /path/to/project
python3 plugins/opinion-manager/runtime/opinion_manager.py context bundle work-email --project /path/to/project
python3 plugins/opinion-manager/runtime/opinion_manager.py context index --project /path/to/project
python3 plugins/opinion-manager/runtime/opinion_manager.py context check --project /path/to/project \
  --signal artifact=email --loaded work-email
```

- `core` 输出常驻规则、自定义段和规则束索引；`bundle` 输出指定规则束；`index` 以 JSON 给出各部分的规则数与字节数。
- `check` 由 Agent 在交付前声明本次交付的信号和已读取的规则束，工具返回应读取与缺失的规则束。缺失时退出码为 1。信号必须取自词表。
- 每次调用先核对锁与正文；核对失败、Profile 没有声明、或 Project Profile 的 `opinion.loading_mode` 设为 `full` 时，命令返回 `fallback`，调用方改为完整读取 `OPINION.md`。前者退出码为 2，后两者为 3。
- 保存前可用 `context core --profile <候选 Profile>` 预览切分结果，此时不读取项目锁。

判断任务属于哪个场景、声明哪些信号，始终由 Agent 负责；判断不清时一并读取。维护规则本身的任务读取完整正文。

## 迁移

`migrate` 把用户**现有**的 Profile（2.0 或 2.1）迁移到带标签声明的 2.2，并把 1.0.0 的公开模板与规则升级到 1.1.0。默认只读预览；写入前备份；失败自动还原；可回滚。不迁移则项目保持原样，2.0 与 2.1 的 Profile、正文与锁字节不变。

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py migrate --project /path/to/project [--tags] [--skip 模板或规则编号]… [--resolutions-file 冲突处理.json] [--output json|markdown]
python3 plugins/opinion-manager/runtime/opinion_manager.py migrate --project /path/to/project [--tags] --apply --confirm <预览指纹>
python3 plugins/opinion-manager/runtime/opinion_manager.py migrate --project /path/to/project --rollback .opinion/backups/<时间戳>-migrate
```

**流程。** 1. 预览：零写入（文件树哈希前后相同，失败路径也一样），输出分类表与 `confirmation_sha256`；2. 向用户展示完整预览并取得明确确认；3. `--apply --confirm <指纹>`：指纹覆盖新 Profile、渲染后的 `OPINION.md`、分类结果与收件箱文件指纹，预览之后 Profile、`OPINION.md`、收件箱或 catalog 任何变化都使它失效，必须重新预览；4. 写入前把 `OPINION.md`、`opinion.lock.json`、原 Profile、收件箱文件原样备份到 `.opinion/backups/<UTC 时间戳>-migrate/files/`，并写 `manifest.json`（相对路径、SHA-256、是否随回滚还原、迁移后的状态）；5. 复用 `profile` → `compose` → `verify` 的原子写，再读回核对 `OPINION.md` 与 Profile；备份、写入、读回任一步失败，按备份逐字节还原并删除本次产生的备份与新目录，项目与迁移前相同（还原本身失败时保留备份目录并给出 `--rollback` 命令）。

**分类（预览逐项列出）。**

- Profile 版本与来源模板版本；可升级的模板与规则沿用 `updates` 的预览，默认升级到最新版本，`--skip` 保持某项不升级，升级冲突沿用 `keep-local` / `use-upstream`（`--resolutions-file`），未处理的冲突阻止写入。只升级一部分模板可能让共享规则落在两个版本上，预览会报错，不写入。
- 2.0：由升级后模板携带的标签声明物化为 2.2。所选模板组合没有可物化的声明（没有选用公开模板、只选了原子规则、模板版本不带标签）：状态 `nothing-to-migrate`，保持完整读取。曾用 `--no-tags` 保持完整读取的 2.0，迁移会再次物化声明，预览的 `notes` 会说明；不迁移保持原样，迁移后可用回滚或 `profile --remove-loading` 恢复完整读取。
- 2.1：默认保持不动（`unchanged`，`context bundle` 继续可用）；加 `--tags` 才改写为 2.2，规则束声明整体替换为标签声明（二者互斥，改写后不再有 `bundles`）。2.2：`unchanged`。
- `OPINION.md` 是否被手工修改（与锁里的正文指纹比较）：自定义规则小节内的任何改动、其他位置的纯追加，原样并入新 Profile 的 `custom`（不改写任何字节，常驻），预览给出追加与改动的行数；改写或删除了生成内容（规则正文、元数据）无法并入，状态 `cannot-migrate`，原因 `manual-edit-conflict`；删除了全部自定义规则同样拒绝，不让已删除的行复活。迁移内部使用与 `compose --accept-current` 相同的确认。CRLF 行尾的正文可迁移，迁移后的正文统一为 LF，回滚逐字节还原原文件。
- `custom` 段逐行归类：`tagged`（已带词表内有效标签，保持原样、按需加载）；`suggest`（带“条件”的结构化行，只在预览里列出，由用户决定是否手工在行尾加 ` [tags …]`）；`resident`（没有标记、手写、认不出、标记损坏或含词表外标签，保持原样，永远常驻）。迁移不替用户猜标签。
- 收件箱：缺 `trigger` / `purpose` 的旧候选只列出，不改写、不阻断迁移；收件箱存储格式、已采纳的行都不改动。
- `loading` 前后对照（`loading.before/after` 摘要与 `changes.loading` 完整声明）、新旧正文差异行数、完整的新 `OPINION.md`（JSON 的 `content`）。

**状态与退出码。**

- 0：`preview`、`migrated`、`unchanged`、`nothing-to-migrate`、`rolled-back`。空白 `OPINION.md`（不存在、空文件或初始化占位且没有锁）是 `nothing-to-migrate`，不是错误。
- 2：用法或 I/O 错误（例如 `--confirm` 没有 `--apply`、未知的 `--skip`）。
- 3：`cannot-migrate`，零写入，输出 `reason` 与 `next`：`no-project`、`no-lock`（没有锁但正文有内容）、`lock-invalid`（锁或 Profile 损坏）、`opinion-missing`、`opinion-unreadable`（符号链接、非普通文件、不可读）、`manual-edit-conflict`、`catalog-conflict`、`draft-failed`、`tag-conflict`、`compose-failed`。
- 4：`refused`，零写入：`confirm-required`、`stale-confirmation`、`unresolved-conflicts`、`blocked`；回滚时为 `not-a-backup`、`manifest-invalid`、`incomplete-backup`、`backup-damaged`、`state-changed`。
- 5：`failed`：`apply-failed`（已还原，`restored: true`，`phase` 为 `backup`、`write`、`read-back` 或 `manifest`）、`restore-failed`（自动还原失败，保留备份目录）、`rollback-failed`（已恢复到回滚前）。

**回滚。** `--rollback <备份目录>` 只接受本项目 `.opinion/backups/` 下的 `*-migrate` 目录，核对 `manifest.json` 与备份文件的 SHA-256；只在当前 `OPINION.md`、锁与迁移写入的 Profile 都等于迁移后的状态时还原 `OPINION.md`、锁与原 Profile，并删除迁移写入的新 Profile，回滚后与迁移前逐字节相同；迁移之后任一文件又被修改则拒绝（`state-changed`，列出文件），不丢后续修改；当前已等于迁移前返回 `unchanged`。回滚直接执行，没有单独的预览。收件箱文件只留底，迁移不改写它，回滚也不还原它。

**不变量（测试逐条断言，见 `tests/test_migrate.py`）。** 预览与失败的应用零写入；用户手工修改的每个字节与 `custom` 的每一行在迁移后仍在 `core` ∪ 全部 `select` 的并集里；没有标记与认不出的行常驻且往返不变；迁移后 `verify` 通过，未选择迁移的 2.0/2.1 字节不变；回滚后 Profile、`OPINION.md`、锁与迁移前逐字节相同；迁移不触碰 `.agents/` 与收件箱的候选内容。重复执行：已是目标状态返回 `unchanged`，不产生新备份。

**已知限制。**

- 用户在“用户自定义规则”小节之外手工追加的小节（含二级标题），迁移后落在 `custom` 段里（该段内的 `## ` 标题保留原样，常驻读取）；`inbox` 按行号读取自定义段时只看到第一个 `## ` 之前的部分。
- 迁移用 `project.json` 的 `language` 渲染新正文：项目语言与旧锁的语言不一致时，新正文换用项目语言。
- 备份目录 `.opinion/backups/` 含原 Profile 与正文，随项目私有保存；是否纳入版本控制由用户决定，迁移不修改 `.gitignore`。
- 只升级一部分模板（`--skip`）可能因共享规则的版本不一致而报 `draft-failed`；这时全部升级，或全部 `--skip`。
- `migrate` 只迁移用户的 Profile 与正文，不触碰 `agents-init` 的项目入口文件。

## 候选、灰度与使用记录

`lifecycle` 命令在项目内的私有目录保存候选、证据快照、人工裁决和灰度使用记录。它只记账，不写 `OPINION.md`、规则目录或 Profile；每个项目各自维护状态，互不引用。

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py lifecycle queue --project /path/to/project --store .opinion/lifecycle
python3 plugins/opinion-manager/runtime/opinion_manager.py lifecycle status --project /path/to/project --store .opinion/lifecycle
python3 plugins/opinion-manager/runtime/opinion_manager.py lifecycle event --project /path/to/project --store .opinion/lifecycle --file 项目内事件.json
```

`event` 默认只预览，返回完整状态变化与 `confirmation_sha256`；追加 `--apply --confirm <指纹>` 才写入。指纹只核对操作完整性，`human_confirmed` 必须来自用户原话。事件与证据文件必须位于项目内，证据在写入时保留不可变副本；原文件之后可以继续增长，副本被改动则阻断后续操作。

事件的 `kind` 与字段：

1. `capture`：`id`、`content`（完整的 `title`、`text`、`scopes`）。同一编号的内容不可修改，修订使用新编号。
2. `observe`：`id`、`occurrence`、`validation`（`pending`、`passed`、`failed`）、`fixture`。
3. `presented`：`id`、`content_sha256`，证据须证明完整内容已展示给用户。
4. `decision`：`id`、`decision`（`accepted`、`rejected`、`held`）、`human_confirmed: true`、`content_sha256`。`accepted` 只允许据此准备 Profile。
5. `adopt`：`human_confirmed: true`、当前 `profile_sha256`。先通过 `verify`，随后该 Profile 版本记为灰度。
6. `use`：`occurrence`、`rules`（当前 Profile 的精确规则引用）、`passed`、`user_changed`、`fixture`。可选字段：
   - `results`：为每条规则给出 `passed`、`not-applicable` 或 `violated`；
   - `loaded_bundles`：本次读取的按需规则束（schema 2.1）；
   - `routing_miss`：本应读取却没有读取的规则束（schema 2.1）；
   - `loaded_tags`：本次读取的标签（schema 2.2，如 `context select --tag` 的取值）；
   - `routing_miss_tags`：本应读取却没有读取的标签（schema 2.2）；
   - `date`：`YYYY-MM-DD`，用于显示规则最近一次使用。
7. `formalize`：`human_confirmed: true`、当前 `profile_sha256`。只改变上线标签，不改规则正文。

计数规则：

- `occurrence` 是项目内工作记录的相对路径，按前两级目录归为一个独立场景，同一任务的多个阶段只计一次。默认接受 `tasks`、`cases`、`reviews`、`projects`、`journal` 五个顶层目录，可用 `--occurrence-root` 改写。
- `fixture: true` 的记录不计入任何成熟度。
- 候选每新增一次独立真实发生次数 n，推荐信用增加 n/(n+1)，达到 1 时标记待推荐，展示后扣 1。出现 `failed` 的候选显示为待处理。这是默认参数，没有经过大量使用验证。
- 标签字段只在 schema 2.2 的 Profile 下可用，取值必须是 `loading.tags[].id` 的不重复列表，未声明的标签、重复项、非列表都会被拒绝并列出有效标签；2.2 下写 `loaded_bundles` 或 `routing_miss` 会报错并指向新字段，2.1 下写标签字段同理。2.1 的字段与语义不变。
- `use` 未通过、用户改动了结果、或存在 `routing_miss` / `routing_miss_tags` 时记为阻断；有阻断的版本不能转正式，须在新版本中解决。
- 至少两个独立的真实成功使用且无阻断时，`status` 标记 `ready_for_review`。它只表示可以提交复盘，不会自动转正式。

`status` 在当前正文通过核验时附带覆盖视图：每条规则的独立成功使用次数、违反次数与最近使用日期，从未使用的规则；Profile 声明了分层加载时，还包括每个规则束被读取和实际用到的次数、漏读次数，以及从未用到的规则束。2.2 的标签单独列在 `tags` 中，每项带 `kind: "tag"`、`loaded_in`、`exercised_in`、`routing_misses`（`exercised_in` 为用到该标签任一成员规则的独立成功使用次数），从未用到的标签列在 `unused_tags`；2.1 的 `bundles` 与 `unused_bundles` 输出不变（条目不带 `kind`，视为规则束）。复盘时据此判断「正式」覆盖到了哪些规则。

同一 Profile 编号发布新版本后，旧版本的使用记录在其全部规则指纹未变时沿用到新版本，并计入独立成功使用次数；规则被修改或个人修订后，相关记录不再沿用。沿用需要读取旧版本的 Profile 文件并核对指纹，默认位置为项目的 `.opinion/profiles`，可用 `--profiles-root` 指定；找不到或指纹不符时不沿用，并在 `not_carried` 中列出。旧版本的阻断不沿用，但违反记录仍可在覆盖视图中看到。

`queue --titles` 只输出尚未裁决候选的标题与适用范围，便于在普通任务中留意是否出现了对应情形；候选全文、证据与次数只在维护规则时展开。

## 私有发布与公开发布

用户维护自己的版本化规则时，先创建私有规则目录，再使用文件编辑工具准备完整 JSON 文件，参考 [规则格式](rule.schema.json) 与 [版本化模板格式](versioned-template.schema.json)。禁止把个人内容写入本仓库。

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py publish \
  --catalog /path/to/private-catalog --file /path/to/release.json
```

先发布模板依赖的精确规则版本，再发布模板。发布模板时检查所有依赖和内部 slot 冲突。

公开目录发布还需要 `--public-approved`。使用此标志前必须执行 Skill 中的公开准入检查、展示待公开文件的全部内容并取得用户明确同意。标志本身不能代替用户的授权。公开目录只接收逐文件批准的规则、模板及说明；审批记录、私有来源映射、个人 Profile、锁和运行日志不能随目录复制到公开包。

## 双语字段与适用条件

规则的 `title`、`text`、`scopes` 与模板的 `name`、`description`、`scopes` 可以写成旧格式（字符串，`scopes` 为字符串数组，视为 zh），也可以写成 `{"zh": …, "en": …}`（`scopes` 为 `{"zh": […], "en": […]}`；至少一种语言，不允许其他语言键）。规则新增可选的 `trigger`（何时适用）、`purpose`（为什么）、`exclude`（何时不适用），同样可本地化。已发布的版本不可修改：旧 1.0.0 文件保持原样，指纹计算不变；给既有规则补译文或字段属于行为变化，不能作为 patch 发布。

- **渲染**：`profile` 预览与 `compose` 按项目文件语言（Project Profile 的 `language`，缺省 zh）选取文字；`trigger` / `purpose` / `exclude` 有就写进规则头一行（zh：`；条件：…；目的：…；不适用：…`，en：`; When: …; Purpose: …; Not for: …`），没有则与旧输出逐字节相同。分层加载的切片与规则束索引里的模板名同样按项目语言选取。
- **untranslated 警告**：项目语言不是 zh 时，预览（`profile`、`compose`、`inbox adopt`）里带 `untranslated`（逐条列出缺该语言译文的公开规则 `ref` 与字段）与 `warnings`。不阻断：缺译文的内容按原文写入，`ref` 点名便于逐条处理；个人修订是用户自己的文字，不计入。zh 项目的输出不带这两个键。
- **发布守卫**：`tests/test_catalog_bilingual.py` 要求公开 catalog 中高于 1.0.0 的规则与模板带三项字段（规则）和 zh / en 两种文字；1.0.0 存量列入显式白名单，改写内容升版时同步从白名单删除。

## 修订、派生与升级

只读查看当前变体的可用新版本：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py updates \
  --catalog /path/to/catalog --profile /path/to/current-profile.json
```

模板内部规则由模板版本精确选择。模板依赖出现新的规则版本时，需要模板发布者发布新的模板版本；`updates` 显示当前模板和直接选择的规则的升级候选。

预览升级：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py profile \
  --catalog /path/to/catalog --project /path/to/project \
  --from-profile /path/to/current-profile.json \
  --id private.my-opinion --version 1.1.0 \
  --template FAMILY/VARIANT@VERSION
```

同系列的新选择替换原选择，其他系列、直接规则和自定义内容继续保留。不同变体之间也可以通过同一命令切换。移除整项选择使用 `--remove-template FAMILY` 或 `--remove-rule ID`；模板内部的单条规则通过 overrides 停用。

私有派生使用独立 Profile 编号，传入 `--from-profile` 并指定新的 `--id` 与版本。派生记录保留原 Profile 编号、版本和指纹；个人 overrides 归属自己的 Profile，公开来源保持原样。

升级对每条个人 overrides 比较三个状态：当前来源快照、个人有效内容、新来源内容。上游改变行为且个人内容存在不同选择时，预览包含完整 `base`、`local`、`upstream`；`unresolved` 非空时禁止保存。

用户确认冲突处理后，通过 `--resolutions-file` 提供规则编号到 `keep-local` 或 `use-upstream` 的映射；或者通过 `--overrides-file` 明确指定完整的新个人内容。处理文件由文件编辑工具写入。上游删除规则时，明确保留的个人规则继续留在 Profile 内。

查看两个已经保存的 Profile 差异：

```bash
python3 plugins/opinion-manager/runtime/opinion_manager.py compare \
  --from-profile /path/to/old-profile.json --to-profile /path/to/new-profile.json
```

修订可先预览新 Profile 与 compose，再集中确认完整结果；两份工具指纹分别用于保存。升级检查、Profile 预览和 Profile 发布都不会直接改变 `OPINION.md`。

## 人工编辑与写入中断

人工编辑的正文继续作为环境的有效内容。`verify` 会报告正文与锁定文件的差异，`compose` 会保护当前文件。

处理步骤：完整读取当前正文；整理用户保留的规则并纳入新 Profile；展示新正文与当前正文的差异；取得用户确认；使用 `--accept-current <当前正文SHA256>` 重新预览并确认写入。指纹可以用 `sha256sum /path/to/project/OPINION.md` 读取。错误指纹不会解除保护。

正文和锁定文件分别使用同目录临时文件原子替换。操作系统中断可能造成两个文件不同步；工具不会声称这种状态已通过核验。读取当前两份文件并确认目标 Profile 后，采用相同的显式当前正文确认流程修复。工具不使用 Git 恢复项目文件。

正文文件缺失且锁定文件完整时，可以用原锁定 Profile 预览并重建完全相同的正文。先恢复原版本的核验结果，再进行升级。

## 验收与实际使用

1. 运行本插件测试：`python3 -m unittest discover -s plugins/opinion-manager/tests -v`。测试在被 Git 和安装器忽略的 `__pycache__/opinion-test-work/` 中创建独立真实文件，使用无个人含义的测试标签。
2. 在独立私有项目使用自定义 Profile，检查预览没有写入；确认后检查三种文件以及 `verify` 结果。
3. 在独立私有规则目录检查同系列两个变体、同变体两个版本的展示与精确选择；检查不同系列的组合和 slot 冲突。
4. 检查 `updates` 只报告候选，旧 Profile 继续生成相同正文；离开规则目录仍可使用 Profile 重建。
5. 检查个人 overrides 与上游新版本产生三方冲突，未确认时保存被拒绝；确认保留个人内容或采用上游内容后核验通过。
6. 人工修改正文，确认 `verify` 报告差异且 `compose` 保留原文件；按导入流程生成新版本后再次核验。
7. 用户确认整个流程后，分别整理生活项目与工作项目的私有规则；每个项目保留一份包含全部适用要求的完整正文。公开模板须单独通过内容审批。

本版本管理已经确认的规则。Agent 可根据已授权记录提出私有候选，用户确认后才发布新版本；候选次数、灰度使用与覆盖情况由 `lifecycle` 命令在项目私有目录记账，工具不会自动晋升候选或把灰度版本转为正式。
