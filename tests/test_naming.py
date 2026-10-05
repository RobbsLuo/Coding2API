"""`provider.naming`：六条渠道统一的模型命名归一。

覆盖两条对外规则（归一键 / 展示名）与它们的共同前置：剥掉上游的免费档
标记与命名空间前缀。分组按「行为」而非按函数切，便于看出归一规则的实际
效果：真实渠道 id（kilo 斜杠、zen 尾缀、各家驼峰）、退化 key（`-free`、
全 free 路径）、上游已给的可读名。
"""

from src.provider.naming import display_model_name, normalize_model_key


class TestNormalizeModelKey:
    """归一键：去 free / 去命名空间前缀 / slug 化。"""

    def test_keeps_plain_id(self):
        """已是干净 slug 的 id 原样返回（小写）。"""
        assert normalize_model_key("glm-5.2") == "glm-5.2"
        assert normalize_model_key("qwen3.8-max") == "qwen3.8-max"
        assert normalize_model_key("v2.6") == "v2.6"

    def test_strips_path_free_segment_and_namespace(self):
        """kilo：去掉命名空间路径段与其中的 free 段，取最后一段。"""
        assert normalize_model_key("kilo-auto/free") == "kilo-auto"
        assert normalize_model_key("openrouter/free") == "openrouter"
        assert normalize_model_key("kilo-offline/free") == "kilo-offline"
        assert normalize_model_key("stealth/space-bunny-alpha") == "space-bunny-alpha"

    def test_strips_colon_free_suffix_and_vendor_namespace(self):
        """kilo 现网形态：`厂商/模型:free` 一次削掉前缀与冒号尾缀。"""
        assert normalize_model_key(
            "poolside/laguna-s-2.1:free") == "laguna-s-2.1"
        assert normalize_model_key(
            "nvidia/nemotron-3-ultra-550b-a55b:free") == "nemotron-3-ultra-550b-a55b"
        assert normalize_model_key("stepfun/step-3.7-flash:free") == "step-3.7-flash"

    def test_strips_vendor_colon_prefix_from_readable_name(self):
        """kilo 展示名带厂商前缀 `Vendor: Model`，削掉后才能与 zen 对齐。"""
        assert normalize_model_key("NVIDIA: Nemotron 3 Ultra") == "nemotron-3-ultra"
        assert normalize_model_key("Qwen: Qwen3.8 27B") == "qwen3.8-27b"
        # 冒号后无空格的写法不算厂商前缀（冒号本身会被 slug 成连字符）
        assert normalize_model_key("qwen3.8:max") == "qwen3.8-max"

    def test_strips_free_suffix(self):
        """zen：免费档尾缀 `-free`（大小写不敏感）。"""
        assert normalize_model_key("longcat-2.5-preview-free") == "longcat-2.5-preview"
        assert normalize_model_key("GAMMA-FREE") == "gamma"

    def test_strips_underscore_free_suffix(self):
        """下划线尾缀同属免费标记（部分上游用 `_free`）。"""
        assert normalize_model_key("qmodel_vl_free") == "qmodel-vl"

    def test_keeps_free_embedded_in_model_name(self):
        """`freeplay` / `freeball` 这类内嵌形式不是免费标记，不能误伤。"""
        assert normalize_model_key("freeplay-2") == "freeplay-2"
        assert normalize_model_key("freeball") == "freeball"

    def test_strips_new_suffix(self):
        """新版标记只删整词尾缀，内嵌形式不动（`newest` 不是标记）。"""
        assert normalize_model_key("ling-3.1-flash-new") == "ling-3.1-flash"
        assert normalize_model_key("Ling 3.1 Flash (new)") == "ling-3.1-flash"
        assert normalize_model_key("Kilo New") == "kilo"
        assert normalize_model_key("gpt-newest") == "gpt-newest"
        assert normalize_model_key("newson-2") == "newson-2"

    def test_stacks_new_and_free_marks(self):
        """标记可叠加：剥到最后一个为止，不能只去最外层一个。"""
        assert normalize_model_key("model-new_free") == "model"
        assert normalize_model_key(
            "Ling 3.1 Flash (new) (free)") == "ling-3.1-flash"
        assert normalize_model_key("a-new-free-new") == "a"

    def test_falls_back_to_raw_when_cleaning_empties_the_key(self):
        """剥完只剩噪声标记时回退原串小写，不能产出空键。"""
        assert normalize_model_key("-free") == "-free"
        assert normalize_model_key("free") == "free"
        assert normalize_model_key("free/free") == "free/free"
        assert normalize_model_key("-new") == "-new"
        assert normalize_model_key("new") == "new"
        assert normalize_model_key("new/new") == "new/new"
        # 只剩一对括号时 slug 化后为空，同样回退原串（不是产出空键）
        assert normalize_model_key("(new)") == "(new)"

    def test_slugifies_whitespace_case_and_punctuation(self):
        """展示名转键：空格/下划线/括号压成连字符，全部小写。"""
        assert normalize_model_key("Kimi K3") == "kimi-k3"
        assert normalize_model_key("Qwen3.8 Max") == "qwen3.8-max"
        assert normalize_model_key("Kilo (offline)") == "kilo-offline"
        assert normalize_model_key("  spaced  out  ") == "spaced-out"
        assert normalize_model_key("UPPER_Snake") == "upper-snake"
        assert normalize_model_key("a--b") == "a-b"

    def test_blank_input_yields_blank_key(self):
        """空串是「拿不到干净名字」的信号，调用方据此回退原始 id。"""
        assert normalize_model_key("") == ""
        assert normalize_model_key("   ") == ""

    def test_keeps_non_ascii_alnum(self):
        """上游名可能有中文后缀：压掉会与短名撞成同一个键，必须保留。"""
        assert normalize_model_key("Kimi K3 长尾") == "kimi-k3-长尾"


class TestDisplayModelName:
    """展示名：首字母大写、品牌纠正大小写、去 free 与斜杠。"""

    def test_derives_name_from_raw_id(self):
        """没有上游名时直接从 id 派生（zen / kilo 多数如此）。"""
        assert display_model_name("longcat-2.5-preview-free") == "LongCat 2.5 Preview"
        assert display_model_name("kimi-k2.7-code-free") == "Kimi K2.7 Code"
        assert display_model_name("kilo-auto/free") == "Kilo Auto"
        assert display_model_name("big-pickle") == "Big Pickle"

    def test_cleans_upstream_readable_name(self):
        """上游已给的可读名走同一条清洗：分隔符转空格、大小写统一。"""
        assert display_model_name("Kimi-K3") == "Kimi K3"
        assert display_model_name("Hy4 preview") == "Hy4 Preview"
        assert display_model_name("Qwen3.8-Max") == "Qwen3.8 Max"
        assert display_model_name("Kilo (offline)") == "Kilo Offline"

    def test_fixes_brand_and_acronym_casing(self):
        """机械 title-case 会写错品牌与缩写，按表纠正。"""
        assert display_model_name("deepseek-v4.1-flash-free") == "DeepSeek V4.1 Flash"
        assert display_model_name("mimo-v2.6-flash-free") == "MiMo V2.6 Flash"
        assert display_model_name("glm-5-free") == "GLM 5"
        assert display_model_name("gpt-5") == "GPT 5"
        assert display_model_name("nemotron-3.5-lightning") == "Nemotron 3.5 Lightning"
        assert display_model_name("qwen3.8-max") == "Qwen3.8 Max"
        assert display_model_name("minimax-m3") == "MiniMax M3"
        assert display_model_name("doubao-seed-2.1-pro") == "Doubao Seed 2.1 Pro"
        # 缩写段全大写
        assert display_model_name("hy4-ocr") == "Hy4 OCR"
        assert display_model_name("k2-vl") == "K2 VL"

    def test_letter_digit_segment_stays_glued(self):
        """字母数字同段不拆（`qwen3.8` 拆开会变成 `Qwen 3.8`）。"""
        assert display_model_name("qwen3.8-max") == "Qwen3.8 Max"
        assert display_model_name("v2.6") == "V2.6"

    def test_idempotent(self):
        """幂等：各渠道 client 可能已自行派生过 name，中心层再洗一次无副作用。"""
        once = display_model_name("longcat-2.5-preview-free")
        assert display_model_name(once) == once
        assert display_model_name(display_model_name("Qwen3.8-Max")) == "Qwen3.8 Max"

    def test_cleans_kilo_vendor_prefix_and_free_marker(self):
        """kilo：削厂商前缀 + 去 free（尾缀 `:free` / 括号词 `(free)`）。"""
        assert display_model_name(
            "NVIDIA: Nemotron 3 Ultra (free)") == "Nemotron 3 Ultra"
        assert display_model_name(
            "Poolside: Laguna S 2.1 (free)") == "Laguna S 2.1"
        assert display_model_name(
            "poolside/laguna-s-2.1:free") == "Laguna S 2.1"
        # 模型名里本来就有 free 的内嵌形式不误伤
        assert display_model_name("freeplay-2") == "Freeplay 2"

    def test_strips_new_marker_from_display_name(self):
        """kilo 新版标记 `(new)` 与厂商前缀一起削（实测 2026-10-05 形态）。"""
        assert display_model_name(
            "inclusionAI: Ling 3.1 Flash (new)") == "Ling 3.1 Flash"
        assert display_model_name("ling-3.1-flash-new") == "Ling 3.1 Flash"
        assert display_model_name("Kilo (new)") == "Kilo"
        # 叠加 free 时仍要削干净
        assert display_model_name(
            "Ling 3.1 Flash (new) (free)") == "Ling 3.1 Flash"
        # 内嵌形式不误伤
        assert display_model_name("gpt-newest") == "GPT Newest"

    def test_pure_noise_yields_empty_name(self):
        """名字整体就是 free / new 之类时返回空串，由调用方回退原始 id。"""
        assert display_model_name("free") == ""
        assert display_model_name("-free") == ""
        assert display_model_name("free/free") == ""
        assert display_model_name("   ") == ""
        assert display_model_name("new") == ""
        assert display_model_name("(new)") == ""

    def test_keeps_non_ascii_alnum(self):
        """中文后缀保留（`长尾` 与短名是不同模型，不能撞键）。"""
        assert display_model_name("Kimi K3 长尾") == "Kimi K3 长尾"


class TestCrossChannelAlignment:
    """同一模型在不同渠道的形态不同，清洗后必须收敛到同一个键。"""

    def test_qoder_internal_id_aligns_by_upstream_name(self):
        """Qoder 内部代号（`kmodel_latest`）只能靠上游名对齐 TRAE/CB。

        归一吃不掉渠道自己的命名（`kimi-k3` vs `kmodel_latest` vs `kimi-k3-1`
        毫无共同字符），对齐靠的是三渠道上游名都是「Kimi-K3」，清洗后收敛到
        同一个键——这也是 `api.models._merge_key` 走展示名而非原 id 的原因。
        """
        assert display_model_name("Kimi-K3") == display_model_name("kimi-k3")
        assert normalize_model_key(display_model_name("Kimi-K3")) == "kimi-k3"
        # 原 id 归一后仍各不相同（内部代号无法靠规则对齐）
        assert normalize_model_key("kimi-k3") != normalize_model_key("kmodel_latest")

    def test_zen_free_variant_aligns_with_paid_variant(self):
        """zen 免费档尾缀只是可用性约定，与付费档同模型。"""
        assert normalize_model_key("deepseek-v4.1-flash-free") == "deepseek-v4.1-flash"
        assert display_model_name("deepseek-v4.1-flash-free") == "DeepSeek V4.1 Flash"

    def test_new_marked_variant_aligns_with_unmarked_variant(self):
        """新版标记与不带标记的写法对上是同一个键，不被拆成两条。

        上游给新版模型打 `(new)` 标记，若不削，同一模型在「带标记」与
        「不带标记」两个渠道写法下会归一成两个键、两条对外 id，用户得选对
        才路由得通。
        """
        marked = normalize_model_key("inclusionAI: Ling 3.1 Flash (new)")
        assert marked == normalize_model_key(display_model_name("Ling 3.1 Flash"))
        assert marked == normalize_model_key("ling-3.1-flash-new")

    def test_zen_and_kilo_nemotron_align(self):
        """zen 与 kilo 的同一 Nemotron 模型：靠**上游展示名**对齐到同一个键。

        两条免费渠道各用不同命名约定与不同 id 规格——zen 的 id 是
        `nemotron-3-ultra-free`，kilo 的是 `nvidia/nemotron-3-ultra-550b-a55b:free`
        （带厂商前缀与参数规格后缀），id 本身对不上；但两边上游名都清洗成
        「Nemotron 3 Ultra」，故 `_merge_key` 走展示名时能并成一条。
        """
        assert normalize_model_key("nemotron-3-ultra-free") == "nemotron-3-ultra"
        assert normalize_model_key("nvidia/nemotron-3-ultra-550b-a55b:free") == \
            "nemotron-3-ultra-550b-a55b"          # id 规格不同，对不上
        # 展示名才是对齐依据
        kilo_label = display_model_name("NVIDIA: Nemotron 3 Ultra (free)")
        assert kilo_label == "Nemotron 3 Ultra"
        assert normalize_model_key(kilo_label) == "nemotron-3-ultra"
        assert normalize_model_key(kilo_label) == \
            normalize_model_key(display_model_name("nemotron-3-ultra-free"))
