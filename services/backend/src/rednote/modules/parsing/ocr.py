"""OCR 引擎抽象（WP17）。

**为什么要有这一层抽象**：本机没有 OCR 引擎（`tesseract` 未安装，
PaddleOCR 需要额外的模型权重与较大依赖），而内网用哪个引擎
**还没定**（研发执行计划 §5 只写了"PaddleOCR"，镜像与版本待核实）。
若不抽象，本包就会写成一个"调不通的分支"，而"扫描件可定位"这条
验收项**在本机永远无法验证**。

抽象之后：
  · `UnavailableOcrEngine`（**默认**）—— 调用即抛错 → 该 unit 记
    `OCR_ENGINE_UNAVAILABLE` → **绝不会变成"成功但内容为空"**；
  · `FakeOcrEngine` —— 确定性输出，供测试与本机验证真实走通
    "扫描件 → 带 bbox 的片段 → 落库 → 回读"这条链路；
  · 内网接入真实引擎时实现同一 Protocol 即可，解析器代码不动。

与 WP12 的 `StorageBackend`、WP13 的 `DeliveryChannel` 是同一手法。

---

## 为什么 `FakeOcrEngine` 会出现在生产代码里（而不是只放测试）

它**只在本机/测试被显式构造**（见 `build_ocr_engine`：只有
`ocr_engine="fake"` 配置才返回它）。放在生产模块里的理由是
"证据脚本与内网联调也要用它造确定性输入" —— 放测试目录的话，
`tools/m1/collect_wp17_evidence.py` 就得从测试目录导入，
而证据脚本不该依赖测试包。

**它不会在任何默认路径上被选中**：`Settings.ocr_engine` 默认 `"none"`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from rednote.modules.parsing.types import OCR_ENGINE_NONE


class OcrEngineUnavailable(RuntimeError):
    """OCR 引擎不可用。

    **刻意不继承 ApiError**：这是部署配置问题（引擎没装/没配），
    不是用户能纠正的请求问题。由 `parsers/image.py` 与 `parsers/pdf.py`
    捕获后转成 unit 级失败 —— 那时它已经变成"这一页没解出来"这个
    对用户可见的事实，不再以异常形式冒到顶层。
    """

    def __init__(self, engine: str) -> None:
        self.engine = engine
        super().__init__(
            f"OCR 引擎不可用：{engine!r}。"
            "扫描件必须经 OCR 才能得到文本；本系统不会把"
            "'识别不出来'降级成'识别结果为空'。"
        )


@dataclass(frozen=True)
class OcrLine:
    """一行识别结果。"""

    text: str
    # 归一化到 0~1 的 `[x0, y0, x1, y1]`。
    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    """一页/一张图的识别结果。

    `lines` 为空 = **这一页确实没有可识别文字**（例如纯色扫描页）。
    这与"引擎不可用"是两件事：前者是 `UNIT_FAILED` 之外的合法结论
    （该 unit 记入 errors，因为没产出片段），后者是
    `OCR_ENGINE_UNAVAILABLE`。
    """

    lines: tuple[OcrLine, ...] = ()
    engine: str = OCR_ENGINE_NONE


@runtime_checkable
class OcrEngine(Protocol):
    """OCR 引擎接口。

    实现方只需保证：**同一输入产生同一输出**（确定性）。
    随机性会让"同一解析器版本结果可复用"这条约束失效 ——
    复用的旧结果与新跑的结果不一致，而对账时无法分辨是哪一个的问题。
    """

    @property
    def engine_name(self) -> str: ...

    def recognize(self, *, image_bytes: bytes) -> OcrResult: ...


class UnavailableOcrEngine:
    """**默认实现**：明确不可用。

    选择"抛错"而不是"返回空结果"是本包最重要的一条判断 ——
    返回空结果会让扫描件的解析**报成功**，而内容一片空白。
    下游（WP18）会在这个空输入上做 Brief 拆解，产出一堆空字段，
    而**没有任何地方能回答"为什么 Brief 是空的"**。
    """

    @property
    def engine_name(self) -> str:
        return OCR_ENGINE_NONE

    def recognize(self, *, image_bytes: bytes) -> OcrResult:
        del image_bytes
        raise OcrEngineUnavailable(self.engine_name)


class FakeOcrEngine:
    """确定性 OCR 引擎（本机验证与测试用）。

    输出**由输入字节的摘要派生**，因此：

      · 同一张图 → 同样的文本与 bbox（可断言）；
      · 不同的图 → 不同的文本（可断言"两个 unit 的片段不同"）。

    **它的输出不是"真实识别结果"**，因此绝不能用于生产。
    名字里的 `Fake` 与 WP13 的 `KIND_FAKE_PROVIDER` 同一用意：
    让它在任何清单里一眼可辨。
    """

    # 归一化 bbox：第 i 行占纵向 1/4，横向留 5% 边距。
    _LINES_PER_PAGE: int = 4

    @property
    def engine_name(self) -> str:
        return "fake-ocr"

    def recognize(self, *, image_bytes: bytes) -> OcrResult:
        import hashlib

        digest = hashlib.sha256(image_bytes).hexdigest()
        lines: list[OcrLine] = []
        for index in range(self._LINES_PER_PAGE):
            y0 = index / self._LINES_PER_PAGE
            y1 = (index + 1) / self._LINES_PER_PAGE
            lines.append(
                OcrLine(
                    # 内容与摘要绑定，且带行号 —— 便于断言"第 2 行的文本"。
                    text=f"[ocr {digest[:8]}] 第 {index + 1} 行识别文本",
                    bbox=(0.05, y0 + 0.01, 0.95, y1 - 0.01),
                    # 递减的置信度（0.95 / 0.90 / 0.85 / 0.80）：
                    # 让"confidence 真的落库了"这件事可断言。
                    confidence=round(0.95 - 0.05 * index, 4),
                )
            )
        return OcrResult(lines=tuple(lines), engine=self.engine_name)


# 配置值 → 实现。**只有 "fake" 会得到 FakeOcrEngine**，
# 其余一切（含拼错的、空的、未来的真引擎名）都是 Unavailable。
# 这个方向是刻意的：**配置错误必须表现为"不可用"，不能表现为"可用"** ——
# 若写成"认不出就退回 Fake"，一个拼错的引擎名会让生产环境用假结果。
_ENGINES: dict[str, type] = {
    "fake": FakeOcrEngine,
}


def build_ocr_engine(name: str | None) -> OcrEngine:
    """按配置构造 OCR 引擎。

    **未知名称 → `UnavailableOcrEngine`**（不是抛异常，也不是退回 Fake）：
    抛异常会让解析流程在装配阶段就崩掉，而"引擎没配"应该是
    "扫描件解不出来"这个业务事实，不是服务起不来。
    """
    key = (name or "").strip().lower()
    factory = _ENGINES.get(key)
    if factory is None:
        return UnavailableOcrEngine()
    return factory()


__all__ = [
    "FakeOcrEngine",
    "OcrEngine",
    "OcrEngineUnavailable",
    "OcrLine",
    "OcrResult",
    "UnavailableOcrEngine",
    "build_ocr_engine",
]
