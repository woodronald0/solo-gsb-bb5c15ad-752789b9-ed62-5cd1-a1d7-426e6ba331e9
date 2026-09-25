"""HTTP Range 回源规划与重组服务（缓存感知）。

POST /plan 接收对象总长度、若干闭区间或 suffix 范围、允许合并的最大间隙
以及单次回源上限，可选地接收一组已缓存块（稳定 id + 闭区间）。服务先按
输入顺序计算缓存的可用覆盖（相互重叠时先出现的缓存块优先），再对未覆盖
片段按 max_gap 合并（绝不跨过缓存覆盖）并按 fetch_limit 拆块，输出回源
块以及每个请求范围在回源块 / 缓存块中的切片映射——一个请求范围可以由
多个来源连续拼成，重叠范围可以复用字节并按映射无歧义重组。不提供
cached_blocks 时，行为与旧版完全一致。

POST /assemble 回显 /plan 的 blocks、mappings（以及 cached_blocks），并
分别按块提供 Base64 回源内容与缓存内容；服务严格校验来源 id、边界、缺块
与重复块后，依据切片映射按原 ranges 顺序重组，返回每段范围的 Base64
内容与长度。

POST /range-response 接收 /assemble 返回的 object_length、ranges 与调用
方指定的媒体类型，生成可供客户端直接下发下载的 206 Partial Content 响应
表示：单段时直接输出范围字节，附 Content-Type、Content-Length 与
Content-Range；多段时按 ranges 下标顺序生成严格 CRLF 分帧的
multipart/byteranges，每段附 Content-Type 与 Content-Range。服务先校验
下标恰好覆盖 0..N-1、区间在对象内且每段 Base64 解码字节数等于声明长度；
Content-Length 按最终二进制正文计算，multipart boundary 随机生成并保证
不会与任何正文分隔行冲突。

POST /verify-fetch-batch 校验按 /plan 的 blocks 逐块回源得到的上游
206 响应：接收 /plan 返回的 blocks、object_length，以及按块编号提供的
上游状态码、Content-Range、ETag 与 Base64 响应体。只接受状态码为 206、
Content-Range 闭区间与规划块及对象总长完全一致、响应体字节数严格相符的
块；缺块、重复块、多余块一律拒绝。所有块必须携带同一个强 ETag（拒绝弱
标签与缺失标签），传入 expected_etag 时还必须与之全等，防止不同版本的
响应混入。通过时按块编号返回可直接提交给 /assemble 的 contents 与确认的
ETag；任何校验失败均返回带块编号与原因的 422，且不回显响应体。

POST /secure-request 是 /plan 与 /assemble 的统一鉴权入口：请求携带
operation（plan 或 assemble）、payload（即原接口请求体）、key_id、
timestamp、nonce、signature；服务按固定 JSON 规范化后用环境配置的
HMAC-SHA256 密钥验签，限制时间偏差，并对 key_id+nonce 做窗口内防重放。
防重放表为有界容量（进行中占位与窗口内已成功 nonce 合计，默认 10000）：
同一把锁内先清理过期项、再识别重放、最后为新 nonce 预留名额；容量已满
且无过期项时返回 503 nonce_capacity_exceeded，绝不驱逐未过期 nonce。
验签通过后执行对应操作并原样返回其响应；/plan 与 /assemble 本身不受影响。

POST /multipart-parse 解析上游返回的 multipart/byteranges 完整响应体
（Base64 传输）：请求提供对象总长度、期望的 interval/suffix ranges 与
实际使用的 boundary。服务严格按 CRLF 切分每个 part（拒绝前导/拖尾字节、
裸 LF、截断与非分隔行），逐 part 校验头部（仅允许 Content-Type 与
Content-Range 且各出现一次，拒绝折行/重复/未知头部等头部注入）、
Content-Range 的 bytes 闭区间与对象总长度、part 字节长度与对象边界；
各 part 的区间必须与期望 ranges 规范化后的区间一一对应（无缺段、多段、
重复段），最后按期望 ranges 顺序返回每段的 Base64 内容。

POST /safe-download-name 解析上游 Content-Disposition 头值，产出可直接
用作本地下载文件名的单一名称：优先采用 RFC 5987 的 filename*（字符集
仅接受 UTF-8 与 ISO-8859-1、百分号字节序列必须合法），其无效或缺失时
回退到传统 filename（token 或 quoted-string，按 UTF-8 优先、ISO-8859-1
兜底解码）；解码后剥离路径前缀（/ 与 \\）、全部控制字符以及尾部的点与
空格，空名与 "."/".." 等危险名称一律拒绝，无法得到有效名称时返回 422。

POST /client-range-decision 是对象代理的前置决策入口：接收对象总长度、
当前强验证器（强 ETag 或 Last-Modified，至少其一）、客户端原始 Range
与可选 If-Range 头值，以及 /plan 的规划参数。服务严格按 RFC 9110 的
字节范围语法解析多段闭区间、开放终点（bytes=500-）与 suffix
（bytes=-500），越界闭区间/开放终点按对象尾端收敛，单个不可满足段被
跳过，全部不可满足时给出 416 决策（Content-Range: bytes */总长）。
If-Range 与当前验证器不匹配（含弱 ETag）时忽略 Range，给出完整对象的
200 决策；匹配或未携带 If-Range 时对可满足范围复用现有缓存感知规划，
给出 206 决策与可直接提交给 /assemble 的 plan。非法头值一律返回带
loc（range_header / if_range / etag / last_modified）的 422；当前
ETag 必须是强验证器，弱标签直接拒绝。

POST /conditional-range-decision 在 /client-range-decision 之上增加
GET 前置条件评估：额外接收客户端 If-None-Match 与 If-Modified-Since。
If-None-Match 支持 "*" 与逗号分隔的 entity-tag 列表，以 RFC 9110 弱比较
（剥掉双方 W/ 前缀后逐字节全等）匹配当前强 ETag；只要 If-None-Match
存在（即使非法）就完全忽略 If-Modified-Since，仅在该头缺席时才按
HTTP 日期（当前 Last-Modified 早于或等于条件时刻）判断是否未修改。
条件命中返回 304 与当前 ETag/Last-Modified 验证器，不解析 Range、不
生成回源计划；未命中时完整复用 /client-range-decision 的
200/206/416 决策，206 的 plan 仍可直接提交 /assemble。非法条件头返回
带 loc（if_none_match / if_modified_since）的 422。

POST /cache-write-decision 供代理在 /verify-fetch-batch 验收单个 206
分块后判断该块能否写入共享缓存：输入对象 URI、块区间、已确认强 ETag、
原请求头以及上游 Cache-Control、Age、Vary；按 HTTP 指令语法解析含引号
的取值，遇 no-store、private、Vary: * 或请求带 Authorization 且响应未
显式允许共享（must-revalidate / public / s-maxage）时拒绝写入。可缓存
时以 s-maxage 优先于 max-age，扣除 Age 得到剩余秒数，过期即拒绝；稳定
缓存键包含 URI、区间、ETag 与 Vary 指定的原请求头值（字段名大小写不
敏感）。返回可缓存判定、原因、剩余秒数与缓存键；非法头值返回带 loc
的 422。

POST /cache-read-plan 是 /cache-write-decision 的读取侧：接收对象 URI、
总长度、当前强 ETag、当前请求头、查询时刻（Unix 秒）、目标 ranges 与
规划参数（max_gap / fetch_limit），以及若干候选缓存块（cache_id、闭
区间、写入时的 cache_key 与 vary_headers、stored_at、remaining_seconds）。
服务按 /cache-write-decision 的键算法，用当前 URI / ETag / 请求头与
候选块的区间、vary_headers 逐块重算缓存键，仅保留键匹配（即对象版本
与 Vary 变体一致）且 stored_at + remaining_seconds 严格晚于查询时刻的
块；可用块按到期时间降序、输入次序升序交给缓存感知 /plan（重叠字节
优先使用更耐久的块），返回可直接用于 /assemble 的 plan，并逐块给出
采用状态与未采用原因（key_mismatch / expired / not_referenced）。
重复 cache_id、非法区间或时间字段返回可定位的 422。
"""

from __future__ import annotations

import base64
import binascii
import bisect
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Annotated, Any, Literal, Union

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

app = FastAPI(
    title="Range 回源规划服务",
    description="为对象存储代理规划可合并、带上限、可感知缓存的 HTTP Range 回源。",
)


# ---------------------- /secure-request 鉴权配置 ----------------------


class SecureRequestConfig:
    """从环境变量加载的 HMAC 验签配置（进程级单例）。

    - SECURE_REQUEST_KEYS：逗号分隔的 ``key_id=secret`` 列表，secret 为
      原始 UTF-8 密钥字节（如含特殊字符建议用仅含字母数字/base64 字符的密钥）。
    - SECURE_REQUEST_TIMESTAMP_SKEW：允许的时间偏差（秒），默认 300。
    - SECURE_REQUEST_NONCE_TTL：nonce 保留时长（秒），默认取 2 倍偏差。
    - SECURE_REQUEST_NONCE_CAPACITY：进程内 nonce 防重放表的容量上限
      （进行中占位与窗口内已成功 nonce 合计），默认 10000。
    """

    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}
        self.raw = os.environ.get("SECURE_REQUEST_KEYS", "")
        for item in self.raw.split(","):
            item = item.strip()
            if not item:
                continue
            key_id, sep, secret = item.partition("=")
            if not sep or not key_id or not secret:
                raise RuntimeError(
                    "SECURE_REQUEST_KEYS 格式非法：每项必须为非空的 'key_id=secret'"
                )
            self.keys[key_id] = secret.encode("utf-8")

        skew = int(os.environ.get("SECURE_REQUEST_TIMESTAMP_SKEW", "300"))
        if skew < 1:
            raise RuntimeError("SECURE_REQUEST_TIMESTAMP_SKEW 必须为正整数")
        self.timestamp_skew = skew

        nonce_ttl = int(
            os.environ.get("SECURE_REQUEST_NONCE_TTL", str(max(1, 2 * skew)))
        )
        if nonce_ttl < 1:
            raise RuntimeError("SECURE_REQUEST_NONCE_TTL 必须为正整数")
        self.nonce_ttl = nonce_ttl

        nonce_capacity = int(os.environ.get("SECURE_REQUEST_NONCE_CAPACITY", "10000"))
        if nonce_capacity < 1:
            raise RuntimeError("SECURE_REQUEST_NONCE_CAPACITY 必须为正整数")
        self.nonce_capacity = nonce_capacity


secure_config = SecureRequestConfig()

# 进程内 nonce 防重放存储：(key_id, nonce) -> (到期时间戳, 占位令牌)。
# 占位令牌随每次预留重新生成：失败请求只能释放自己的占位，避免在其占位
# 过期后误删被并发请求重新占用（甚至已成功）的同名记录。
_nonce_lock = threading.Lock()
_nonce_seen: dict[tuple[str, str], tuple[float, int]] = {}


def _sweep_nonces(now: float) -> None:
    """清理已过期 nonce（调用方需持有 _nonce_lock）。"""
    expired = [k for k, (expiry, _token) in _nonce_seen.items() if expiry <= now]
    for k in expired:
        del _nonce_seen[k]


def _reserve_nonce(
    nonce_key: tuple[str, str], now: float
) -> tuple[str, tuple[float, int] | None]:
    """在同一把锁内完成防重放判断与名额预留（调用方需持有 _nonce_lock）。

    顺序固定为：先清理过期项 → 再识别重放 → 最后为新 nonce 预留名额。
    容量统计同时包含进行中的占位与窗口内已成功的 nonce；容量已满且没有
    任何过期项可清理时返回 ``("full", None)``（调用方据此返回 503，绝不
    驱逐未过期 nonce）；重放返回 ``("replayed", None)``（409 优先于 503）；
    预留成功返回 ``("reserved", (到期时间戳, 占位令牌))``。
    """
    _sweep_nonces(now)
    if nonce_key in _nonce_seen:
        return "replayed", None
    if len(_nonce_seen) >= secure_config.nonce_capacity:
        return "full", None
    token = secrets.randbits(64)
    expiry = now + secure_config.nonce_ttl
    reservation = (expiry, token)
    _nonce_seen[nonce_key] = reservation
    return "reserved", reservation


def _release_nonce(
    nonce_key: tuple[str, str], reservation: tuple[float, int]
) -> None:
    """释放本次失败请求自己的占位（调用方需持有 _nonce_lock）。

    仅当存储中的记录与本次预留（到期时间戳 + 占位令牌）逐字一致时才删除；
    若该记录已过期并被并发请求重新占用，则保持不动。
    """
    if _nonce_seen.get(nonce_key) == reservation:
        del _nonce_seen[nonce_key]


# ----------------------------- 公共类型 -----------------------------


def _validate_cache_id(v: object) -> object:
    """cache_id 允许字符串或整数，但拒绝布尔值与空字符串。"""
    if isinstance(v, bool):
        raise ValueError("cache_id 不得为布尔值")
    if isinstance(v, str) and not v:
        raise ValueError("cache_id 不能为空字符串")
    return v


CacheId = Annotated[Union[str, int], BeforeValidator(_validate_cache_id)]


class CachedBlock(BaseModel):
    """已缓存的字节块：稳定 id + 闭区间 [start, end]。"""

    cache_id: CacheId = Field(..., description="缓存块的稳定 id，用于在 /assemble 中对应内容")
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存结束字节偏移（含）")


# ----------------------------- 请求模型 -----------------------------


class IntervalRange(BaseModel):
    """闭区间范围 [start, end]，两端均为包含端点的字节偏移。"""

    type: Literal["interval"]
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="结束字节偏移（含）")


class SuffixRange(BaseModel):
    """suffix 范围：对象末尾的 last_n 个字节。"""

    type: Literal["suffix"]
    last_n: Annotated[int, StrictInt] = Field(..., ge=1, description="请求末尾的字节数")


RangeSpec = Annotated[
    Union[IntervalRange, SuffixRange],
    Field(discriminator="type"),
]


class PlanRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    ranges: list[RangeSpec] = Field(..., min_length=1, description="请求范围列表，可为闭区间或 suffix")
    max_gap: Annotated[int, StrictInt] = Field(..., ge=0, description="允许合并的最大间隙（字节）")
    fetch_limit: Annotated[int, StrictInt] = Field(..., ge=1, description="单次回源上限（字节）")
    cached_blocks: list[CachedBlock] = Field(
        default_factory=list,
        description="可选的已缓存块列表；相互重叠时按输入顺序优先",
    )

    @model_validator(mode="after")
    def _check_unique_cache_ids(self) -> "PlanRequest":
        seen: set[object] = set()
        for cb in self.cached_blocks:
            if cb.cache_id in seen:
                raise ValueError(f"cached_blocks 中 cache_id {cb.cache_id!r} 重复")
            seen.add(cb.cache_id)
        return self


# ----------------------------- 响应模型 -----------------------------


class SliceOut(BaseModel):
    source: Literal["fetch", "cache"] = Field(
        "fetch", description="切片来源：fetch=回源块，cache=缓存块"
    )
    block_index: int | None = Field(None, description="source=fetch 时的回源块编号")
    cache_id: CacheId | None = Field(None, description="source=cache 时的缓存块 id")
    block_start: int = Field(..., description="来源块起始字节偏移（含）")
    block_end: int = Field(..., description="来源块结束字节偏移（含）")
    offset_in_block: int = Field(..., description="切片在来源块中的起始偏移")
    length: int = Field(..., ge=1, description="该切片字节数")


class RangeMapping(BaseModel):
    index: int = Field(..., description="该范围在原请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    slices: list[SliceOut] = Field(
        ..., description="在各来源块中的切片映射，按对象偏移连续覆盖整个范围"
    )


class FetchBlock(BaseModel):
    index: int
    start: int = Field(..., description="回源起始字节偏移（含）")
    end: int = Field(..., description="回源结束字节偏移（含）")
    length: int = Field(..., description="回源字节数，不超过 fetch_limit")
    range_header: str = Field(..., description="可直接用于回源的 HTTP Range 头值")


class PlanResponse(BaseModel):
    object_length: int
    blocks: list[FetchBlock]
    mappings: list[RangeMapping]
    cached_blocks: list[CachedBlock] = Field(
        default_factory=list,
        description="本次规划实际引用到的缓存块（原样回显其声明信息）",
    )


# ------------------------ /assemble 请求/响应模型 ------------------------


class BlockContent(BaseModel):
    block_index: int = Field(..., description="回源块编号，对应 blocks[].index")
    data_b64: str = Field(..., description="该回源块的完整回源内容（标准 Base64）")


class CacheContent(BaseModel):
    cache_id: CacheId = Field(..., description="缓存块 id，对应 cached_blocks[].cache_id")
    data_b64: str = Field(..., description="该缓存块的完整内容（标准 Base64）")


class AssembleRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    blocks: list[FetchBlock] = Field(
        ..., description="回显 /plan 返回的 blocks（全部命中缓存时可为空列表）"
    )
    mappings: list[RangeMapping] = Field(..., min_length=1, description="回显 /plan 返回的 mappings")
    contents: list[BlockContent] = Field(
        ..., description="按块提供的 Base64 回源内容（无回源块时为空列表）"
    )
    cached_blocks: list[CachedBlock] = Field(
        default_factory=list, description="回显 /plan 返回的 cached_blocks"
    )
    cache_contents: list[CacheContent] = Field(
        default_factory=list, description="按缓存块提供的 Base64 缓存内容"
    )


class AssembledRange(BaseModel):
    index: int = Field(..., description="该范围在原请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    length: int = Field(..., ge=1, description="重组后的字节数")
    data_b64: str = Field(..., description="重组后的范围内容（标准 Base64）")


class AssembleResponse(BaseModel):
    object_length: int
    ranges: list[AssembledRange] = Field(
        ..., description="按原 ranges 顺序（mappings[].index）排列的重组结果"
    )


# ---------------- /verify-fetch-batch 请求/响应模型 ----------------


class UpstreamBlockResponse(BaseModel):
    """单个回源块的上游响应要素（按块编号提供）。"""

    block_index: int = Field(..., description="回源块编号，对应 blocks[].index")
    status_code: Annotated[int, StrictInt] = Field(
        ..., ge=100, le=599, description="上游 HTTP 状态码（仅 206 可通过校验）"
    )
    content_range: str = Field(
        ...,
        min_length=1,
        description="上游 Content-Range 头原值，形如 'bytes start-end/total'",
    )
    etag: str | None = Field(
        None,
        description="上游 ETag 头原值；必须存在且为强 ETag（不得以 W/ 开头）",
    )
    body_b64: str = Field(..., description="该块上游响应体的标准 Base64")


class VerifyFetchBatchRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    blocks: list[FetchBlock] = Field(
        ..., description="回显 /plan 返回的 blocks（全部命中缓存时为空列表）"
    )
    responses: list[UpstreamBlockResponse] = Field(
        ..., description="按块编号提供的各回源块上游响应要素"
    )
    expected_etag: str | None = Field(
        None,
        description="可选：调用方先前确认过的强 ETag；提供时所有块必须与之全等",
    )


class VerifiedBlockContent(BaseModel):
    """校验通过的单块内容：字段与 /assemble 的 contents 元素完全一致。"""

    block_index: int = Field(..., description="回源块编号，对应 blocks[].index")
    data_b64: str = Field(..., description="该回源块的完整回源内容（标准 Base64）")


class VerifyFetchBatchResponse(BaseModel):
    object_length: int
    contents: list[VerifiedBlockContent] = Field(
        ...,
        description="按块编号升序排列，可直接作为 /assemble 的 contents 提交",
    )
    etag: str | None = Field(
        ...,
        description="所有块一致携带的强 ETag（去首尾 OWS 后的原值，含引号）；"
        "blocks 为空时为 null",
    )


# ------------------- /multipart-parse 请求/响应模型 -------------------


_MULTIPART_BOUNDARY_MAX_LENGTH = 70


class MultipartParseRequest(BaseModel):
    """multipart/byteranges 解析请求。

    body_b64 为上游完整响应体（含所有 boundary 行与 part 头）的标准
    Base64 编码；boundary 为实际用在 boundary 分隔行中的值（即上游
    Content-Type: multipart/byteranges; boundary=... 中的 boundary）。
    """

    object_length: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="对象总字节长度（必须与 Content-Range 的 total 一致）"
    )
    ranges: list[RangeSpec] = Field(
        ..., min_length=1, description="期望返回的范围列表（闭区间或 suffix），决定输出顺序"
    )
    boundary: str = Field(
        ...,
        min_length=1,
        max_length=_MULTIPART_BOUNDARY_MAX_LENGTH,
        description="multipart 分隔行中的 boundary 值（不含前导 --）",
    )
    expected_content_type: str | None = Field(
        None,
        description="可选：若提供，每个 part 的 Content-Type 去除首尾空白后必须与之完全一致（大小写不敏感）",
    )
    body_b64: str = Field(..., description="上游完整 multipart/byteranges 响应体（标准 Base64）")

    @field_validator("boundary")
    @classmethod
    def _check_boundary_chars(cls, v: str) -> str:
        # RFC 2046 bchars：不含空白与 tspecials（" ( ) , / : ; < = > ? @ [ ] \\）。
        if not _BOUNDARY_RE.fullmatch(v):
            raise ValueError(
                "boundary 非法：仅允许 RFC 2046 bcharsnospace 字符，"
                '不得含空白或 " ( ) , : ; < > @ [ ] \\ 等字符'
            )
        return v


class MultipartRange(BaseModel):
    index: int = Field(..., description="该范围在请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    length: int = Field(..., ge=1, description="解析出的字节数")
    data_b64: str = Field(..., description="该段内容（标准 Base64）")


class MultipartParseResponse(BaseModel):
    object_length: int
    boundary: str = Field(..., description="实际采用的 boundary（回显）")
    parts: int = Field(..., ge=1, description="解析到的 part 数量")
    ranges: list[MultipartRange] = Field(
        ..., description="按请求 ranges 顺序排列的各段内容"
    )


# ---------------------- /secure-request 请求模型 ----------------------


class SecureRequest(BaseModel):
    """/secure-request 的签名信封；payload 为 /plan 或 /assemble 的原请求体。"""

    operation: Literal["plan", "assemble"] = Field(
        ..., description="要执行的操作：plan 或 assemble"
    )
    payload: dict[str, Any] = Field(
        ..., description="对应操作的原始请求体（/plan 或 /assemble 的 JSON）"
    )
    key_id: str = Field(..., min_length=1, max_length=128, description="密钥 id")
    timestamp: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="Unix 秒级时间戳"
    )
    nonce: str = Field(
        ..., min_length=1, max_length=128, description="一次性随机串，防重放"
    )
    signature: str = Field(
        ..., min_length=1, description="规范化签名串的 HMAC-SHA256（小写十六进制）"
    )


# ----------------------------- 错误处理 -----------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """以统一的 422 结构报告字段校验失败（越界、类型错误等）。"""
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            # ctx 中可能携带校验器抛出的异常对象，统一转成字符串以便序列化
            "detail": jsonable_encoder(exc.errors(), custom_encoder={Exception: str}),
        },
    )


class PlanningError(Exception):
    """语义校验失败（范围不可满足等），对应 HTTP 422。"""

    def __init__(self, message: str, range_index: int | None = None):
        self.message = message
        self.range_index = range_index
        super().__init__(message)


@app.exception_handler(PlanningError)
async def planning_error_handler(request: Request, exc: PlanningError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "unsatisfiable",
            "message": exc.message,
            **({"range_index": exc.range_index} if exc.range_index is not None else {}),
        },
    )


class AssemblyError(Exception):
    """组装阶段的语义校验失败（缺块、Base64 无效、越界等），对应 HTTP 422。

    loc 为可定位的字段路径，例如 "contents[2].data_b64"；
    block_index / cache_id / range_index / slice_index 在适用时给出。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        block_index: int | None = None,
        cache_id: str | int | None = None,
        range_index: int | None = None,
        slice_index: int | None = None,
        code: str = "assembly_error",
    ):
        self.message = message
        self.loc = loc
        self.block_index = block_index
        self.cache_id = cache_id
        self.range_index = range_index
        self.slice_index = slice_index
        self.code = code
        super().__init__(message)


@app.exception_handler(AssemblyError)
async def assembly_error_handler(request: Request, exc: AssemblyError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.block_index is not None:
        detail["block_index"] = exc.block_index
    if exc.cache_id is not None:
        detail["cache_id"] = exc.cache_id
    if exc.range_index is not None:
        detail["range_index"] = exc.range_index
    if exc.slice_index is not None:
        detail["slice_index"] = exc.slice_index
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


class MultipartParseError(Exception):
    """multipart/byteranges 解析与校验失败，统一对应 HTTP 422。

    loc 为可定位的字段路径：
      - "body_b64"：整体响应体或 Base64 本身的问题；
      - "ranges[i]"：期望范围规范化后的问题（区间非法、越界、重复等）；
      - "parts[p].<header>"：第 p 个 part（从 0 起）的结构/头部/范围问题。
    range_index / part_index 在适用时给出。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        part_index: int | None = None,
        range_index: int | None = None,
        start: int | None = None,
        end: int | None = None,
        code: str = "multipart_parse_error",
    ):
        self.message = message
        self.loc = loc
        self.part_index = part_index
        self.range_index = range_index
        self.start = start
        self.end = end
        self.code = code
        super().__init__(message)


@app.exception_handler(MultipartParseError)
async def multipart_parse_error_handler(request: Request, exc: MultipartParseError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.part_index is not None:
        detail["part_index"] = exc.part_index
    if exc.range_index is not None:
        detail["range_index"] = exc.range_index
    if exc.start is not None:
        detail["start"] = exc.start
    if exc.end is not None:
        detail["end"] = exc.end
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


class BatchVerifyError(Exception):
    """回源批次校验失败（非 206、范围/长度不符、ETag 不一致、缺块等），HTTP 422。

    loc 为可定位的字段路径，例如 "responses[2].content_range"、
    "expected_etag"；block_index 在可归属到某个回源块时给出；
    extra 用于附带补充字段（如 missing 块编号列表）。错误响应绝不回显
    任何响应体字节或 Base64。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        block_index: int | None = None,
        code: str = "fetch_batch_error",
        extra: dict[str, object] | None = None,
    ):
        self.message = message
        self.loc = loc
        self.block_index = block_index
        self.code = code
        self.extra = extra
        super().__init__(message)


@app.exception_handler(BatchVerifyError)
async def batch_verify_error_handler(request: Request, exc: BatchVerifyError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.block_index is not None:
        detail["block_index"] = exc.block_index
    if exc.extra:
        detail.update(exc.extra)
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


# ----------------------------- 核心规划逻辑 -----------------------------


def normalize(req: PlanRequest, enforce_fetch_limit: bool) -> list[tuple[int, int]]:
    """把每个范围规范化为闭区间 [start, end]，并拒绝越界或不可满足输入。

    enforce_fetch_limit 为 True 时（无缓存的旧行为），单个范围长度不得
    超过 fetch_limit；缓存感知规划允许一个范围拆到多个回源块，不做此限制。
    """
    normalized: list[tuple[int, int]] = []
    for i, spec in enumerate(req.ranges):
        if spec.type == "interval":
            start, end = spec.start, spec.end
            if end < start:
                raise PlanningError("end 不得小于 start", i)
            if end >= req.object_length:
                raise PlanningError(
                    f"区间 [{start},{end}] 越界：对象长度为 {req.object_length}", i
                )
        else:  # suffix
            # suffix 在对象不足 last_n 字节时按 HTTP 语义回落到整个对象
            start = max(0, req.object_length - spec.last_n)
            end = req.object_length - 1
            if start > end:
                raise PlanningError("空对象无法满足 suffix 范围请求", i)

        if enforce_fetch_limit and end - start + 1 > req.fetch_limit:
            raise PlanningError(
                f"范围 [{start},{end}] 长度 {end - start + 1} 超过单次回源上限 "
                f"{req.fetch_limit}，无法规划",
                i,
            )
        normalized.append((start, end))
    return normalized


def build_plan(
    normalized: list[tuple[int, int]], max_gap: int, fetch_limit: int
) -> tuple[list[tuple[int, int]], list[int]]:
    """贪心合并回源区间（无缓存时的原始逻辑）。

    按起始偏移排序后逐个并入当前块，仅当：
      - 与当前块的间隙 <= max_gap，且
      - 合并后的并集长度 <= fetch_limit
    时才合并；否则另起一个回源块。任何结果块都不会超过 fetch_limit。

    返回 (回源块列表, 每个原范围所属块的编号列表)。
    """
    order = sorted(range(len(normalized)), key=lambda i: normalized[i])
    blocks: list[list[int]] = []  # 每块是 (start, end)
    owner: list[int] = [0] * len(normalized)

    cur_start = cur_end = -1
    cur_index = -1

    for i in order:
        start, end = normalized[i]
        if cur_start == -1:
            cur_start, cur_end, cur_index = start, end, 0
            blocks.append([cur_start, cur_end])
            owner[i] = cur_index
            continue

        gap = start - cur_end - 1
        merged_length = max(cur_end, end) - cur_start + 1
        if gap <= max_gap and merged_length <= fetch_limit:
            # 落入或紧邻当前块：合并（包含关系时块不变）。
            if end > cur_end:
                cur_end = end
                blocks[cur_index][1] = end
        else:
            # 间隙过大或合并后超过上限：另起一个回源块。
            cur_start, cur_end = start, end
            cur_index = len(blocks)
            blocks.append([cur_start, cur_end])
        owner[i] = cur_index

    return [(s, e) for s, e in blocks], owner


# ----------------------------- 缓存覆盖与区间工具 -----------------------------


def subtract_intervals(
    start: int, end: int, intervals: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从闭区间 [start, end] 中扣除已排序、互不重叠的区间，返回剩余闭区间。"""
    free: list[tuple[int, int]] = []
    cursor = start
    for s, e in intervals:
        if e < cursor:
            continue
        if s > end:
            break
        if s > cursor:
            free.append((cursor, min(s - 1, end)))
        cursor = max(cursor, e + 1)
        if cursor > end:
            break
    if cursor <= end:
        free.append((cursor, end))
    return free


def insert_interval(
    intervals: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    """把闭区间 [start, end] 并入已排序的互不相交区间列表（重叠或相邻会合并）。"""
    merged: list[tuple[int, int]] = []
    placed = False
    for s, e in intervals:
        if e + 1 < start:
            merged.append((s, e))
        elif end + 1 < s:
            if not placed:
                merged.append((start, end))
                placed = True
            merged.append((s, e))
        else:
            start, end = min(start, s), max(end, e)
    if not placed:
        merged.append((start, end))
    return merged


def compute_coverage(
    cached_blocks: list[CachedBlock],
) -> tuple[list[tuple[int, int, object, int, int]], list[tuple[int, int]]]:
    """按输入顺序计算缓存的可用覆盖：相互重叠时先出现的缓存块优先。

    返回 (覆盖条目, 覆盖区间)：
      - 覆盖条目为 (start, end, cache_id, block_start, block_end)，按起始偏移
        排序；block_start/block_end 是所属缓存块声明的完整区间，用于切片定位；
      - 覆盖区间为排序后的 (start, end) 列表，用于扣除与相交判断。
    """
    claimed: list[tuple[int, int]] = []
    entries: list[tuple[int, int, object, int, int]] = []
    for cb in cached_blocks:
        # 只把尚未被更早缓存块占据的部分记为该块的可用覆盖
        for fs, fe in subtract_intervals(cb.start, cb.end, claimed):
            entries.append((fs, fe, cb.cache_id, cb.start, cb.end))
        claimed = insert_interval(claimed, cb.start, cb.end)
    entries.sort(key=lambda item: (item[0], item[1]))
    return entries, [(s, e) for s, e, *_ in entries]


def has_coverage(
    cov: list[tuple[int, int]], cov_starts: list[int], start: int, end: int
) -> bool:
    """闭区间 [start, end]（start <= end）是否与任一缓存覆盖区间相交。"""
    i = bisect.bisect_right(cov_starts, end) - 1
    return i >= 0 and cov[i][1] >= start


def merge_fragments(
    fragments: list[tuple[int, int]],
    max_gap: int,
    cov: list[tuple[int, int]],
    cov_starts: list[int],
) -> list[tuple[int, int]]:
    """把未覆盖片段按 max_gap 贪心合并，但合并不得跨过任何缓存覆盖。"""
    merged: list[list[int]] = []
    for fs, fe in fragments:  # 调用前已按 (start, end) 排序
        if merged:
            prev_end = merged[-1][1]
            gap = fs - prev_end - 1
            if gap <= max_gap and (
                gap < 1 or not has_coverage(cov, cov_starts, prev_end + 1, fs - 1)
            ):
                # 间隙足够小且不含缓存覆盖：并入当前段（包含关系时段不变）。
                if fe > prev_end:
                    merged[-1][1] = fe
                continue
        merged.append([fs, fe])
    return [(s, e) for s, e in merged]


def split_blocks(
    merged: list[tuple[int, int]], fetch_limit: int
) -> list[tuple[int, int]]:
    """把合并后的回源段按 fetch_limit 拆成不超过上限的回源块。"""
    blocks: list[tuple[int, int]] = []
    for start, end in merged:
        cursor = start
        while cursor <= end:
            block_end = min(end, cursor + fetch_limit - 1)
            blocks.append((cursor, block_end))
            cursor = block_end + 1
    return blocks


# ----------------------------- /plan -----------------------------


def plan_without_cache(req: PlanRequest) -> PlanResponse:
    """不含缓存块时的原始规划逻辑（保持旧行为与旧约束）。"""
    normalized = normalize(req, enforce_fetch_limit=True)
    block_tuples, owner = build_plan(normalized, req.max_gap, req.fetch_limit)

    fetch_blocks = [
        FetchBlock(
            index=i,
            start=s,
            end=e,
            length=e - s + 1,
            range_header=f"bytes={s}-{e}",
        )
        for i, (s, e) in enumerate(block_tuples)
    ]

    mappings: list[RangeMapping] = []
    for i, (start, end) in enumerate(normalized):
        block_index = owner[i]
        bstart, bend = block_tuples[block_index]
        # 排序合并保证该范围完整包含在其所属块中，映射只有一个切片。
        slices = [
            SliceOut(
                source="fetch",
                block_index=block_index,
                block_start=bstart,
                block_end=bend,
                offset_in_block=start - bstart,
                length=end - start + 1,
            )
        ]
        mappings.append(
            RangeMapping(
                index=i,
                type=req.ranges[i].type,
                start=start,
                end=end,
                slices=slices,
            )
        )

    return PlanResponse(
        object_length=req.object_length,
        blocks=fetch_blocks,
        mappings=mappings,
        cached_blocks=[],
    )


def plan_with_cache(req: PlanRequest) -> PlanResponse:
    """缓存感知规划：先算可用覆盖，再合并未覆盖片段，最后按上限拆块。"""
    for cb in req.cached_blocks:
        if cb.end < cb.start:
            raise PlanningError(
                f"缓存块 {cb.cache_id!r} 的区间 [{cb.start},{cb.end}] 非法："
                "end 不得小于 start"
            )
        if cb.end >= req.object_length:
            raise PlanningError(
                f"缓存块 {cb.cache_id!r} [{cb.start},{cb.end}] 越界："
                f"对象长度为 {req.object_length}"
            )

    # 缓存命中部分可复用，单个范围不再受 fetch_limit 整体限制
    normalized = normalize(req, enforce_fetch_limit=False)
    coverage, cov_intervals = compute_coverage(req.cached_blocks)
    cov_starts = [s for s, _ in cov_intervals]

    # 1. 每个范围扣除缓存覆盖，得到未覆盖片段
    fragments: list[tuple[int, int]] = []
    for start, end in normalized:
        fragments.extend(subtract_intervals(start, end, cov_intervals))
    fragments.sort()

    # 2. 片段按 max_gap 合并（不跨过缓存覆盖），再按 fetch_limit 拆块
    block_tuples = split_blocks(
        merge_fragments(fragments, req.max_gap, cov_intervals, cov_starts),
        req.fetch_limit,
    )

    fetch_blocks = [
        FetchBlock(
            index=i,
            start=s,
            end=e,
            length=e - s + 1,
            range_header=f"bytes={s}-{e}",
        )
        for i, (s, e) in enumerate(block_tuples)
    ]

    # 3. 缓存覆盖与回源块共同组成有序来源段，恰好拼接每个请求范围
    segments: list[tuple[int, int, str, object, int, int]] = []
    for cs, ce, cid, bs, be in coverage:
        segments.append((cs, ce, "cache", cid, bs, be))
    for i, (bs, be) in enumerate(block_tuples):
        segments.append((bs, be, "fetch", i, bs, be))
    segments.sort(key=lambda seg: (seg[0], seg[1]))

    mappings: list[RangeMapping] = []
    used_cache_ids: set[object] = set()
    for i, (start, end) in enumerate(normalized):
        slices: list[SliceOut] = []
        for ss, se, kind, ref, bs, be in segments:
            if se < start or ss > end:
                continue
            a, b = max(start, ss), min(end, se)
            if a > b:
                continue
            if kind == "cache":
                used_cache_ids.add(ref)
            slices.append(
                SliceOut(
                    source=kind,  # type: ignore[arg-type]
                    block_index=ref if kind == "fetch" else None,
                    cache_id=ref if kind == "cache" else None,
                    block_start=bs,
                    block_end=be,
                    offset_in_block=a - bs,
                    length=b - a + 1,
                )
            )
        mappings.append(
            RangeMapping(
                index=i,
                type=req.ranges[i].type,
                start=start,
                end=end,
                slices=slices,
            )
        )

    used_cached_blocks = [
        cb for cb in req.cached_blocks if cb.cache_id in used_cache_ids
    ]
    return PlanResponse(
        object_length=req.object_length,
        blocks=fetch_blocks,
        mappings=mappings,
        cached_blocks=used_cached_blocks,
    )


@app.post("/plan", response_model=PlanResponse)
def plan(req: PlanRequest) -> PlanResponse:
    if not req.cached_blocks:
        return plan_without_cache(req)
    return plan_with_cache(req)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ----------------------------- 组装逻辑 -----------------------------


def decode_b64(loc: str, raw: str, **ctx: object) -> bytes:
    """严格按标准 Base64（含正确的 = 填充）解码单个来源块内容。"""
    if len(raw) % 4 != 0:
        raise AssemblyError(
            "Base64 长度不是 4 的倍数，缺少填充或包含非法字符",
            loc,
            code="invalid_base64",
            **ctx,
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssemblyError(
            f"Base64 解码失败：{exc}",
            loc,
            code="invalid_base64",
            **ctx,
        )


@app.post("/assemble", response_model=AssembleResponse)
def assemble(req: AssembleRequest) -> AssembleResponse:
    # 1. 校验 blocks：编号唯一、区间合法、不超过对象长度。
    declared: dict[int, FetchBlock] = {}
    for pos, block in enumerate(req.blocks):
        if block.index < 0:
            raise AssemblyError(
                "回源块编号不得为负",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="invalid_block",
            )
        if block.end < block.start:
            raise AssemblyError(
                f"回源块 [{block.start},{block.end}] 非法：end 不得小于 start",
                f"blocks[{pos}]",
                block_index=block.index,
                code="invalid_block",
            )
        if block.end >= req.object_length:
            raise AssemblyError(
                f"回源块 [{block.start},{block.end}] 越界：对象长度为 {req.object_length}",
                f"blocks[{pos}]",
                block_index=block.index,
                code="block_out_of_bounds",
            )
        if block.length != block.end - block.start + 1:
            raise AssemblyError(
                f"声明长度 {block.length} 与区间 [{block.start},{block.end}] "
                f"应有的 {block.end - block.start + 1} 字节不符",
                f"blocks[{pos}].length",
                block_index=block.index,
                code="invalid_block",
            )
        if block.index in declared:
            raise AssemblyError(
                f"回源块编号 {block.index} 重复",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="duplicate_block",
            )
        declared[block.index] = block

    # 2. 校验 contents：索引唯一且与 blocks 一一对应，再解码并核对字节数。
    decoded: dict[int, bytes] = {}
    for pos, item in enumerate(req.contents):
        if item.block_index < 0:
            raise AssemblyError(
                "内容块编号不得为负",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="invalid_block",
            )
        if item.block_index in decoded:
            raise AssemblyError(
                f"回源块 {item.block_index} 的内容被重复提供",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="duplicate_block_content",
            )
        block = declared.get(item.block_index)
        if block is None:
            raise AssemblyError(
                f"提供了 blocks 中不存在的多余回源块 {item.block_index}",
                f"contents[{pos}].block_index",
                block_index=item.block_index,
                code="unexpected_block",
            )
        data = decode_b64(
            f"contents[{pos}].data_b64", item.data_b64, block_index=item.block_index
        )
        if len(data) != block.length:
            raise AssemblyError(
                f"块 {block.index} 解码后 {len(data)} 字节，"
                f"与声明长度 {block.length} 不符",
                f"contents[{pos}].data_b64",
                block_index=block.index,
                code="block_length_mismatch",
            )
        decoded[item.block_index] = data

    missing = [idx for idx in declared if idx not in decoded]
    if missing:
        missing.sort()
        raise AssemblyError(
            f"缺少 {len(missing)} 个回源块的内容：{missing}",
            "contents",
            block_index=missing[0],
            code="missing_block",
        )

    # 2b. 校验 cached_blocks：id 唯一、区间合法、不超过对象长度。
    declared_cache: dict[object, CachedBlock] = {}
    for pos, cb in enumerate(req.cached_blocks):
        if cb.end < cb.start:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} 的区间 [{cb.start},{cb.end}] 非法："
                "end 不得小于 start",
                f"cached_blocks[{pos}]",
                cache_id=cb.cache_id,
                code="invalid_cache",
            )
        if cb.end >= req.object_length:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} [{cb.start},{cb.end}] 越界："
                f"对象长度为 {req.object_length}",
                f"cached_blocks[{pos}]",
                cache_id=cb.cache_id,
                code="cache_out_of_bounds",
            )
        if cb.cache_id in declared_cache:
            raise AssemblyError(
                f"缓存块 id {cb.cache_id!r} 重复",
                f"cached_blocks[{pos}].cache_id",
                cache_id=cb.cache_id,
                code="duplicate_cache",
            )
        declared_cache[cb.cache_id] = cb

    # 2c. 校验 cache_contents：id 唯一且与 cached_blocks 一一对应，再解码核对字节数。
    decoded_cache: dict[object, bytes] = {}
    for pos, item in enumerate(req.cache_contents):
        if item.cache_id in decoded_cache:
            raise AssemblyError(
                f"缓存块 {item.cache_id!r} 的内容被重复提供",
                f"cache_contents[{pos}].cache_id",
                cache_id=item.cache_id,
                code="duplicate_cache_content",
            )
        cb = declared_cache.get(item.cache_id)
        if cb is None:
            raise AssemblyError(
                f"提供了 cached_blocks 中不存在的缓存块 {item.cache_id!r} 的内容",
                f"cache_contents[{pos}].cache_id",
                cache_id=item.cache_id,
                code="unexpected_cache",
            )
        data = decode_b64(
            f"cache_contents[{pos}].data_b64", item.data_b64, cache_id=item.cache_id
        )
        cache_length = cb.end - cb.start + 1
        if len(data) != cache_length:
            raise AssemblyError(
                f"缓存块 {cb.cache_id!r} 解码后 {len(data)} 字节，"
                f"与声明长度 {cache_length} 不符",
                f"cache_contents[{pos}].data_b64",
                cache_id=cb.cache_id,
                code="cache_length_mismatch",
            )
        decoded_cache[item.cache_id] = data

    missing_cache = [cid for cid in declared_cache if cid not in decoded_cache]
    if missing_cache:
        raise AssemblyError(
            f"缺少 {len(missing_cache)} 个缓存块的内容：{missing_cache}",
            "cache_contents",
            cache_id=missing_cache[0],
            code="missing_cache",
        )

    # 3. 校验 mappings：下标连续且覆盖 0..N-1，切片不越界且恰好拼接成原范围。
    mapping_pos: dict[int, int] = {}
    for pos, mapping in enumerate(req.mappings):
        if mapping.index < 0:
            raise AssemblyError(
                "映射下标不得为负",
                f"mappings[{pos}].index",
                range_index=mapping.index,
                code="invalid_mapping_index",
            )
        if mapping.index in mapping_pos:
            raise AssemblyError(
                f"范围下标 {mapping.index} 在 mappings 中重复",
                f"mappings[{pos}].index",
                range_index=mapping.index,
                code="duplicate_mapping_index",
            )
        mapping_pos[mapping.index] = pos

    n = len(req.mappings)
    if set(mapping_pos) != set(range(n)):
        extra = sorted(i for i in mapping_pos if i >= n)
        missing_idx = sorted(set(range(n)) - set(mapping_pos))
        detail = f"mappings 下标应为 0..{n - 1} 的全排列"
        if missing_idx:
            detail += f"，缺少下标 {missing_idx}"
        if extra:
            detail += f"，存在多余下标 {extra}"
        raise AssemblyError(detail, "mappings", code="invalid_mapping_index")

    ordered_mappings = sorted(req.mappings, key=lambda m: m.index)
    for mapping in ordered_mappings:
        mpos = mapping_pos[mapping.index]
        if mapping.end < mapping.start:
            raise AssemblyError(
                f"范围 [{mapping.start},{mapping.end}] 非法：end 不得小于 start",
                f"mappings[{mpos}]",
                range_index=mapping.index,
                code="invalid_mapping",
            )
        if mapping.end >= req.object_length:
            raise AssemblyError(
                f"范围 [{mapping.start},{mapping.end}] 越界："
                f"对象长度为 {req.object_length}",
                f"mappings[{mpos}]",
                range_index=mapping.index,
                code="mapping_out_of_bounds",
            )
        if not mapping.slices:
            raise AssemblyError(
                "切片列表不能为空",
                f"mappings[{mpos}].slices",
                range_index=mapping.index,
                code="empty_slices",
            )

        cursor = mapping.start
        for slice_pos, sl in enumerate(mapping.slices):
            loc_base = f"mappings[{mpos}].slices[{slice_pos}]"
            # 解析切片来源，并核对来源字段组合与声明的块区间
            if sl.source == "fetch":
                if sl.block_index is None or sl.cache_id is not None:
                    raise AssemblyError(
                        "source=fetch 的切片必须携带 block_index 且不得携带 cache_id",
                        loc_base,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                block = declared.get(sl.block_index)
                if block is None:
                    raise AssemblyError(
                        f"切片引用了不存在的回源块 {sl.block_index}",
                        f"{loc_base}.block_index",
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="unknown_block_reference",
                    )
                src_start, src_end, src_length = block.start, block.end, block.length
                src_ctx: dict[str, object] = {"block_index": block.index}
            else:  # cache
                if sl.cache_id is None or sl.block_index is not None:
                    raise AssemblyError(
                        "source=cache 的切片必须携带 cache_id 且不得携带 block_index",
                        loc_base,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="invalid_slice",
                    )
                cb = declared_cache.get(sl.cache_id)
                if cb is None:
                    raise AssemblyError(
                        f"切片引用了未声明的缓存块 {sl.cache_id!r}",
                        f"{loc_base}.cache_id",
                        cache_id=sl.cache_id,
                        range_index=mapping.index,
                        slice_index=slice_pos,
                        code="unknown_cache_reference",
                    )
                src_start, src_end = cb.start, cb.end
                src_length = src_end - src_start + 1
                src_ctx = {"cache_id": cb.cache_id}

            if sl.block_start != src_start or sl.block_end != src_end:
                raise AssemblyError(
                    f"切片记录的来源块区间 [{sl.block_start},{sl.block_end}] "
                    f"与声明的 [{src_start},{src_end}] 不一致",
                    loc_base,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_block_mismatch",
                    **src_ctx,
                )
            if sl.offset_in_block < 0 or sl.length < 1:
                raise AssemblyError(
                    "切片的 offset_in_block 不得为负且 length 至少为 1",
                    loc_base,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="invalid_slice",
                    **src_ctx,
                )
            if sl.offset_in_block + sl.length > src_length:
                raise AssemblyError(
                    f"切片 offset={sl.offset_in_block}, length={sl.length} "
                    f"超出来源块的 {src_length} 字节",
                    loc_base,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_out_of_bounds",
                    **src_ctx,
                )
            expected_offset = cursor - src_start
            if sl.offset_in_block != expected_offset:
                raise AssemblyError(
                    f"切片应从来源块的偏移 {expected_offset} 开始"
                    f"（衔接对象偏移 {cursor}），实际偏移为 {sl.offset_in_block}，"
                    "切片无法连续覆盖该范围",
                    loc_base,
                    range_index=mapping.index,
                    slice_index=slice_pos,
                    code="slice_misaligned",
                    **src_ctx,
                )
            cursor += sl.length

        expected_length = mapping.end - mapping.start + 1
        assembled_length = cursor - mapping.start
        if assembled_length != expected_length:
            raise AssemblyError(
                f"范围 {mapping.index} 的切片总长 {assembled_length} 字节，"
                f"与声明的 {expected_length} 字节不符",
                f"mappings[{mpos}].slices",
                range_index=mapping.index,
                code="assembly_length_mismatch",
            )

    # 4. 全部通过：按切片从解码字节中取材，按原 ranges 顺序拼接。
    results: list[AssembledRange] = []
    for mapping in ordered_mappings:
        parts = []
        for sl in mapping.slices:
            data = (
                decoded[sl.block_index]
                if sl.source == "fetch"
                else decoded_cache[sl.cache_id]
            )
            parts.append(data[sl.offset_in_block : sl.offset_in_block + sl.length])
        payload = b"".join(parts)
        results.append(
            AssembledRange(
                index=mapping.index,
                type=mapping.type,
                start=mapping.start,
                end=mapping.end,
                length=len(payload),
                data_b64=base64.b64encode(payload).decode("ascii"),
            )
        )

    return AssembleResponse(object_length=req.object_length, ranges=results)


# --------------------------- /verify-fetch-batch ---------------------------

# RFC 9110 opaque-tag：DQUOTE *etagc DQUOTE。etagc 为 0x21 / 0x23-0x7E /
# obs-text(0x80-0xFF)——不含空格、控制字符与双引号本身；反斜杠在 ETag 中
# 没有转义语义，0x5C 作为普通字面字符允许。
_STRONG_ETAG_RE = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]*"$')


def _parse_strong_etag(
    raw: str | None,
    loc: str,
    *,
    block_index: int | None = None,
    expected: bool = False,
) -> str:
    """校验并归一化强 ETag，返回去首尾 OWS 后的 opaque-tag 原值。

    缺失/空白、弱标签（W/ 前缀，大小写不敏感）与非法 opaque-tag 一律拒绝；
    expected=True 时使用 expected_etag 专属错误码。
    """
    if raw is None or not raw.strip(" \t"):
        raise BatchVerifyError(
            "缺少 ETag 响应头，无法确认回源块版本",
            loc,
            block_index=block_index,
            code="missing_etag",
        )
    value = raw.strip(" \t")
    if value[:2].lower() == "w/":
        if expected:
            raise BatchVerifyError(
                "expected_etag 不允许是弱 ETag（W/ 前缀）：弱验证器不能保证"
                "字节级一致",
                loc,
                code="weak_expected_etag",
            )
        raise BatchVerifyError(
            "仅接受强 ETag：弱 ETag（W/ 前缀）不能保证字节级一致，"
            "不得与其他块混合重组",
            loc,
            block_index=block_index,
            code="weak_etag",
        )
    if not _STRONG_ETAG_RE.fullmatch(value):
        if expected:
            raise BatchVerifyError(
                "expected_etag 不是合法的强 ETag：必须为双引号包裹的 "
                "opaque-tag（如 \"v1\"），且不得为弱标签",
                loc,
                code="invalid_expected_etag",
            )
        raise BatchVerifyError(
            "ETag 不是合法的强 ETag：必须为双引号包裹的 opaque-tag（如 "
            '"v1"），不得包含空格、控制字符或裸引号',
            loc,
            block_index=block_index,
            code="invalid_etag",
        )
    return value


def _decode_b64_batch(raw: str, loc: str, block_index: int) -> bytes:
    """严格按标准 Base64（含正确填充、validate=True）解码单块上游响应体。"""
    if len(raw) % 4 != 0:
        raise BatchVerifyError(
            "Base64 长度不是 4 的倍数，缺少填充或包含非法字符",
            loc,
            block_index=block_index,
            code="invalid_base64",
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BatchVerifyError(
            f"Base64 解码失败：{exc}",
            loc,
            block_index=block_index,
            code="invalid_base64",
        )


@app.post("/verify-fetch-batch", response_model=VerifyFetchBatchResponse)
def verify_fetch_batch(req: VerifyFetchBatchRequest) -> VerifyFetchBatchResponse:
    """逐块校验上游 206 响应与 /plan 规划的一致性及强 ETag 版本一致性。

    通过时按块编号升序返回可直接用于 /assemble.contents 的数据与全批确认的
    强 ETag；任何失败都抛出带块编号与原因的 BatchVerifyError（422），
    错误响应中不包含任何响应体字节或 Base64。
    """
    # 0. expected_etag 先做独立的强 ETag 语法校验（与具体块无关）。
    expected_etag: str | None = None
    if req.expected_etag is not None:
        expected_etag = _parse_strong_etag(
            req.expected_etag, "expected_etag", expected=True
        )

    # 1. 校验回显的 blocks 自身：编号唯一、区间合法、length 自洽且不越界。
    declared: dict[int, FetchBlock] = {}
    for pos, block in enumerate(req.blocks):
        if block.index < 0:
            raise BatchVerifyError(
                "回源块编号不得为负",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="invalid_block_index",
            )
        if block.end < block.start:
            raise BatchVerifyError(
                f"回源块 [{block.start},{block.end}] 非法：end 不得小于 start",
                f"blocks[{pos}]",
                block_index=block.index,
                code="invalid_block",
            )
        if block.end >= req.object_length:
            raise BatchVerifyError(
                f"回源块 [{block.start},{block.end}] 越界："
                f"对象长度为 {req.object_length}",
                f"blocks[{pos}]",
                block_index=block.index,
                code="block_out_of_bounds",
            )
        if block.length != block.end - block.start + 1:
            raise BatchVerifyError(
                f"声明长度 {block.length} 与区间 [{block.start},{block.end}] "
                f"应有的 {block.end - block.start + 1} 字节不符",
                f"blocks[{pos}].length",
                block_index=block.index,
                code="invalid_block_length",
            )
        if block.index in declared:
            raise BatchVerifyError(
                f"回源块编号 {block.index} 在 blocks 中重复",
                f"blocks[{pos}].index",
                block_index=block.index,
                code="duplicate_block",
            )
        declared[block.index] = block

    # 2. 按 responses 输入顺序逐块校验上游响应要素。
    items_by_index: dict[int, UpstreamBlockResponse] = {}
    confirmed_etag: str | None = None
    for pos, item in enumerate(req.responses):
        loc_base = f"responses[{pos}]"
        if item.block_index < 0:
            raise BatchVerifyError(
                "响应块编号不得为负",
                f"{loc_base}.block_index",
                block_index=item.block_index,
                code="invalid_block_index",
            )
        if item.block_index in items_by_index:
            raise BatchVerifyError(
                f"回源块 {item.block_index} 的上游响应被重复提供",
                f"{loc_base}.block_index",
                block_index=item.block_index,
                code="duplicate_response",
            )
        block = declared.get(item.block_index)
        if block is None:
            raise BatchVerifyError(
                f"提供了 blocks 中不存在的多余回源块 {item.block_index} 的响应",
                f"{loc_base}.block_index",
                block_index=item.block_index,
                code="unexpected_block",
            )

        # 2a. 仅接受 206 Partial Content：200/416 等一律拒绝，防止整对象
        #     或错误响应被当作分块内容混入。
        if item.status_code != 206:
            raise BatchVerifyError(
                f"回源块 {block.index} 的上游状态码为 {item.status_code}，"
                "仅接受 206 Partial Content",
                f"{loc_base}.status_code",
                block_index=block.index,
                code="non_206_status",
            )

        # 2b. Content-Range 必须严格为 bytes start-end/total，且闭区间与
        #     规划块、total 与对象总长完全一致。
        cr_value = item.content_range.strip(" \t")
        try:
            cr_match = _CONTENT_RANGE_RE.fullmatch(cr_value.encode("ascii"))
        except UnicodeEncodeError:
            cr_match = None
        if cr_match is None:
            raise BatchVerifyError(
                "Content-Range 不符合 'bytes start-end/total' 格式"
                "（不接受 * 通配形式）",
                f"{loc_base}.content_range",
                block_index=block.index,
                code="invalid_content_range",
            )
        cr_start, cr_end, cr_total = (
            int(cr_match.group(1)),
            int(cr_match.group(2)),
            int(cr_match.group(3)),
        )
        if cr_end < cr_start:
            raise BatchVerifyError(
                "Content-Range 闭区间非法：last-byte-pos 不得小于 "
                "first-byte-pos",
                f"{loc_base}.content_range",
                block_index=block.index,
                code="invalid_content_range",
            )
        if cr_total != req.object_length:
            raise BatchVerifyError(
                f"Content-Range 声明的对象总长度 {cr_total} 与请求的 "
                f"{req.object_length} 不一致",
                f"{loc_base}.content_range",
                block_index=block.index,
                code="content_range_total_mismatch",
            )
        if (cr_start, cr_end) != (block.start, block.end):
            raise BatchVerifyError(
                f"Content-Range 闭区间 [{cr_start},{cr_end}] 与规划块 "
                f"[{block.start},{block.end}] 不一致",
                f"{loc_base}.content_range",
                block_index=block.index,
                code="content_range_mismatch",
            )

        # 2c. 响应体必须为合法标准 Base64，解码字节数严格等于块长度
        #     （即 Content-Range 声明长度，二者已在上面证明相等）。
        data = _decode_b64_batch(
            item.body_b64, f"{loc_base}.body_b64", block.index
        )
        if len(data) != block.length:
            raise BatchVerifyError(
                f"回源块 {block.index} 响应体解码后 {len(data)} 字节，"
                f"与规划长度 {block.length}（Content-Range 闭区间 "
                f"[{block.start},{block.end}]）不符",
                f"{loc_base}.body_b64",
                block_index=block.index,
                code="body_length_mismatch",
            )

        # 2d. 每块必须携带强 ETag，且全批逐字节相同（不同版本不得混入）。
        strong_etag = _parse_strong_etag(
            item.etag, f"{loc_base}.etag", block_index=block.index
        )
        if confirmed_etag is None:
            confirmed_etag = strong_etag
        elif strong_etag != confirmed_etag:
            raise BatchVerifyError(
                f"回源块 {block.index} 的 ETag 与此前已确认块的强 ETag "
                "不一致：不同对象版本的分块不得混合重组",
                f"{loc_base}.etag",
                block_index=block.index,
                code="etag_mismatch",
            )

        items_by_index[item.block_index] = item

    # 3. expected_etag 与全批确认到的强 ETag 必须全等。
    if expected_etag is not None and confirmed_etag != expected_etag:
        if confirmed_etag is None:
            raise BatchVerifyError(
                "没有任何回源块（blocks/responses 为空），无法确认 expected_etag",
                "expected_etag",
                code="expected_etag_unverifiable",
            )
        raise BatchVerifyError(
            "本批回源块确认到的强 ETag 与 expected_etag 不一致："
            "对象版本已变化，不得按旧规划重组",
            "expected_etag",
            block_index=next(iter(items_by_index)),
            code="expected_etag_mismatch",
        )

    # 4. 存在性核对：每个规划块都必须恰好有一份响应（缺块拒绝）。
    missing = sorted(set(declared) - set(items_by_index))
    if missing:
        raise BatchVerifyError(
            f"缺少 {len(missing)} 个回源块的上游响应：{missing}",
            "responses",
            block_index=missing[0],
            code="missing_blocks",
            extra={"missing": missing},
        )

    # 5. 全部通过：按块编号升序输出，body_b64 已在上文通过严格解码校验，
    #    可原样作为 /assemble 的 contents 元素提交。
    contents = [
        VerifiedBlockContent(
            block_index=idx, data_b64=items_by_index[idx].body_b64
        )
        for idx in sorted(items_by_index)
    ]
    return VerifyFetchBatchResponse(
        object_length=req.object_length,
        contents=contents,
        etag=confirmed_etag,
    )


# --------------------------- /multipart-parse ---------------------------

# RFC 2046 bcharsnospace：DIGIT / ALPHA / 部分标点（不含空格与 tspecials）。
_BOUNDARY_RE = re.compile(r"^[0-9A-Za-z'()+_,-./:=?]{1,70}$")
# token（RFC 7230）：除分隔符与空白、控制字符外的可见 ASCII。
_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# quoted-string 取值（不含外层引号）：qdtext（含 tab）或 quoted-pair。
_QUOTED_VALUE_RE = re.compile(rb'^(?:[\t\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\t\x20-\x7e])*$')
# Content-Range: bytes <start>-<end>/<total>（仅数字 total，不接受 *）。
_CONTENT_RANGE_RE = re.compile(rb"^bytes[ \t]+(\d+)-(\d+)/(\d+)$")
_CRLF = b"\r\n"
_BLANK_CRLF = b"\r\n\r\n"

# 每个 part 允许出现的头部（全部小写）；任何其他头部都按头部注入拒绝。
_ALLOWED_PART_HEADERS = frozenset({"content-type", "content-range"})


def _decode_b64_multipart(raw: str) -> bytes:
    """严格按标准 Base64（含正确填充、validate=True）解码完整响应体。"""
    if len(raw) % 4 != 0:
        raise MultipartParseError(
            "Base64 长度不是 4 的倍数，缺少填充或包含非法字符",
            "body_b64",
            code="invalid_base64",
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MultipartParseError(
            f"Base64 解码失败：{exc}",
            "body_b64",
            code="invalid_base64",
        )


def _normalize_expected_ranges(req: MultipartParseRequest) -> list[tuple[int, int]]:
    """把期望 ranges 规范化为闭区间，校验合法性、对象边界与唯一性。"""
    normalized: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for i, spec in enumerate(req.ranges):
        if spec.type == "interval":
            start, end = spec.start, spec.end
            if end < start:
                raise MultipartParseError(
                    "end 不得小于 start",
                    f"ranges[{i}]",
                    range_index=i,
                    start=start,
                    end=end,
                    code="invalid_range",
                )
            if end >= req.object_length:
                raise MultipartParseError(
                    f"区间 [{start},{end}] 越界：对象长度为 {req.object_length}",
                    f"ranges[{i}]",
                    range_index=i,
                    start=start,
                    end=end,
                    code="range_out_of_bounds",
                )
        else:  # suffix
            start = max(0, req.object_length - spec.last_n)
            end = req.object_length - 1
            if start > end:
                raise MultipartParseError(
                    "空对象无法满足 suffix 范围请求",
                    f"ranges[{i}]",
                    range_index=i,
                    code="unsatisfiable_suffix",
                )
        if (start, end) in seen:
            raise MultipartParseError(
                f"期望范围 [{start},{end}] 在 ranges 中重复，multipart 中无法出现重复段",
                f"ranges[{i}]",
                range_index=i,
                start=start,
                end=end,
                code="duplicate_expected_range",
            )
        seen.add((start, end))
        normalized.append((start, end))
    return normalized


def _validate_content_type(raw_value: bytes, part_index: int, loc: str) -> str:
    """校验 Content-Type 头部取值的语法，返回去首尾空白后的小写值。

    接受 `token "/" token *( OWS ";" OWS parameter )`，参数值可为 token
    或 quoted-string；不允许折行、裸 LF、空值、非法字符。
    """
    if not raw_value or any(c in raw_value for c in b"\r\n"):
        raise MultipartParseError(
            "Content-Type 取值为空或包含换行",
            loc,
            part_index=part_index,
            code="malformed_header",
        )
    value = raw_value.strip(b" \t")
    if not value:
        raise MultipartParseError(
            "Content-Type 取值为空",
            loc,
            part_index=part_index,
            code="malformed_header",
        )
    segments = value.split(b";")
    main_type = segments[0]
    mtype = main_type.split(b"/")
    if (
        len(mtype) != 2
        or not _TOKEN_RE.fullmatch(mtype[0])
        or not _TOKEN_RE.fullmatch(mtype[1])
    ):
        raise MultipartParseError(
            f"Content-Type {value!r} 不是合法的 type/subtype",
            loc,
            part_index=part_index,
            code="invalid_content_type",
        )
    for seg in segments[1:]:
        param = seg.strip(b" \t")
        if not param or b"=" not in param:
            raise MultipartParseError(
                f"Content-Type 参数 {seg!r} 非法",
                loc,
                part_index=part_index,
                code="invalid_content_type",
            )
        name, _, pvalue = param.partition(b"=")
        name = name.strip(b" \t")
        pvalue = pvalue.strip(b" \t")
        if not _TOKEN_RE.fullmatch(name):
            raise MultipartParseError(
                f"Content-Type 参数名 {name!r} 非法",
                loc,
                part_index=part_index,
                code="invalid_content_type",
            )
        if pvalue.startswith(b'"') and pvalue.endswith(b'"'):
            if len(pvalue) < 2 or not _QUOTED_VALUE_RE.fullmatch(pvalue[1:-1]):
                raise MultipartParseError(
                    "Content-Type 参数 quoted-string 非法",
                    loc,
                    part_index=part_index,
                    code="invalid_content_type",
                )
        elif not _TOKEN_RE.fullmatch(pvalue):
            raise MultipartParseError(
                f"Content-Type 参数值 {pvalue!r} 非法",
                loc,
                part_index=part_index,
                code="invalid_content_type",
            )
    return value.decode("ascii").lower()


def _parse_part_headers(
    header_block: bytes, part_index: int
) -> tuple[tuple[int, int, int], str]:
    """按 CRLF 严格解析单个 part 的头部块，返回 (Content-Range, Content-Type)。

    - 每个头部必须是 `name: value`，name 为合法 token；
    - 仅允许 Content-Type 与 Content-Range，且各自恰好出现一次；
    - 拒绝折叠（折行）、空名、空值与任何重复/未知头部（头部注入）。
    """
    loc = f"parts[{part_index}].headers"
    if not header_block:
        raise MultipartParseError(
            "part 缺少头部块（空行之前没有任何头部）",
            loc,
            part_index=part_index,
            code="missing_headers",
        )
    seen: set[str] = set()
    content_range: tuple[int, int, int] | None = None
    content_type: str | None = None

    for line_no, line in enumerate(header_block.split(_CRLF)):
        hloc = f"{loc}[{line_no}]"
        if not line or line[:1] in (b" ", b"\t"):
            # 空行在此前已被 split(blank CRLF) 排除；前导空白即折叠/非法行。
            raise MultipartParseError(
                "头部出现空行或折叠（obs-fold），拒绝解析",
                hloc,
                part_index=part_index,
                code="header_injection",
            )
        if b":" not in line:
            raise MultipartParseError(
                f"头部行 {line!r} 缺少 ':'",
                hloc,
                part_index=part_index,
                code="malformed_header",
            )
        name, _, value = line.partition(b":")
        if not _TOKEN_RE.fullmatch(name):
            raise MultipartParseError(
                f"头部名 {name!r} 非法",
                hloc,
                part_index=part_index,
                code="malformed_header",
            )
        # value 允许 token 前后 OWS，但不允许裸 CR/LF（split 已保证无 CRLF）
        # 及其他控制字符。
        stripped = value.strip(b" \t")
        if any(ch < 0x20 or ch == 0x7F for ch in stripped):
            raise MultipartParseError(
                f"头部 {name!r} 的取值包含控制字符",
                hloc,
                part_index=part_index,
                code="header_injection",
            )

        lname = name.decode("ascii").lower()
        if lname not in _ALLOWED_PART_HEADERS:
            raise MultipartParseError(
                f"part 不允许出现 {name.decode('ascii', 'replace')!r} 头部"
                "（仅允许 Content-Type 与 Content-Range）",
                hloc,
                part_index=part_index,
                code="unexpected_header",
            )
        if lname in seen:
            raise MultipartParseError(
                f"part 中 {name.decode('ascii', 'replace')!r} 头部重复出现",
                hloc,
                part_index=part_index,
                code="duplicate_header",
            )
        seen.add(lname)

        if lname == "content-type":
            content_type = _validate_content_type(
                value, part_index, f"parts[{part_index}].content_type"
            )
        else:  # content-range
            crloc = f"parts[{part_index}].content_range"
            m = _CONTENT_RANGE_RE.fullmatch(stripped)
            if m is None:
                raise MultipartParseError(
                    f"Content-Range {stripped!r} 不符合 'bytes start-end/total' 格式",
                    crloc,
                    part_index=part_index,
                    code="invalid_content_range",
                )
            content_range = (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    if "content-range" not in seen:
        raise MultipartParseError(
            "part 缺少 Content-Range: bytes start-end/total 头部",
            f"parts[{part_index}].content_range",
            part_index=part_index,
            code="missing_content_range",
        )
    if "content-type" not in seen:
        raise MultipartParseError(
            "part 缺少 Content-Type 头部",
            f"parts[{part_index}].content_type",
            part_index=part_index,
            code="missing_content_type",
        )
    assert content_range is not None and content_type is not None
    return content_range, content_type


def _split_multipart_parts(body: bytes, boundary: str) -> list[tuple[bytes, bytes]]:
    """按 RFC 2046 风格严格切分 multipart 正文，返回 [(header_block, payload)]。

    严格要求：
      - 正文必须以 "--" + boundary 开头（拒绝前导 preamble/字节）；
      - 所有边界行只能以 CRLF 结束（拒绝裸 LF）；
      - 最后必须以 "--" + boundary + "--" 收尾，其后不得再有任何字节
        （拒绝 epilogue/拖尾字节）；
      - 每个 part 必须含 CRLFCRLF 分隔头部与正文，头部块不得为二进制
        （裸 CR 等非法行由头部解析进一步拒绝）。
    """
    delim = b"--" + boundary.encode("ascii")
    if not body.startswith(delim):
        raise MultipartParseError(
            "响应体必须以 '--' + boundary 开头（不允许前导 preamble 或字节）",
            "body_b64",
            code="bad_boundary",
        )
    pos = len(delim)
    if body[pos : pos + 2] == _CRLF:
        closing = False
        pos += 2
    elif body[pos : pos + 2] == b"--":
        # 开边界立即结束：multipart 中没有任何 part。
        if body[pos + 2 :]:
            raise MultipartParseError(
                "结束边界 '--boundary--' 之后存在拖尾字节（不允许 epilogue）",
                "body_b64",
                code="trailing_bytes",
            )
        raise MultipartParseError(
            "multipart 响应中没有任何 part",
            "body_b64",
            code="empty_multipart",
        )
    else:
        raise MultipartParseError(
            "boundary 分隔行后必须紧跟 CRLF（不接受裸 LF 或其他字节）",
            "body_b64",
            code="bad_boundary_line",
        )

    parts: list[tuple[bytes, bytes]] = []
    part_index = 0
    while True:
        # 下一处边界：正文以 CRLF "--boundary" 开始，正文内出现的边界
        # 序列一律视为结构错误（byteranges 正文不应内嵌边界行）。
        next_at = body.find(_CRLF + delim, pos)
        if next_at == -1:
            raise MultipartParseError(
                "响应体被截断：未找到下一个 boundary 分隔行",
                "body_b64",
                part_index=part_index,
                code="truncated_multipart",
            )
        raw_part = body[pos:next_at]
        sep_at = raw_part.find(_BLANK_CRLF)
        if sep_at == -1:
            raise MultipartParseError(
                "part 缺少头部与正文之间的 CRLFCRLF 分隔",
                f"parts[{part_index}]",
                part_index=part_index,
                code="missing_header_terminator",
            )
        parts.append((raw_part[:sep_at], raw_part[sep_at + 4 :]))

        pos = next_at + 2 + len(delim)  # 跳过 CRLF 与 boundary，定位到后续字节
        tail = body[pos : pos + 2]
        if tail == b"--":
            pos += 2
            if body[pos:]:
                raise MultipartParseError(
                    "结束边界 '--boundary--' 之后存在拖尾字节（不允许 epilogue）",
                    "body_b64",
                    code="trailing_bytes",
                )
            closing = True
            break
        if tail != _CRLF:
            raise MultipartParseError(
                "boundary 分隔行后必须紧跟 CRLF（不接受裸 LF 或其他字节）",
                "body_b64",
                part_index=part_index + 1,
                code="bad_boundary_line",
            )
        pos += 2
        part_index += 1

    if not closing:  # 理论不可达，防御性检查
        raise MultipartParseError(
            "响应体被截断：缺少结束边界 '--boundary--'",
            "body_b64",
            code="truncated_multipart",
        )
    return parts


@app.post("/multipart-parse", response_model=MultipartParseResponse)
def multipart_parse(req: MultipartParseRequest) -> MultipartParseResponse:
    """解析并严格校验 multipart/byteranges 响应，按期望 ranges 顺序返回内容。"""
    expected = _normalize_expected_ranges(req)
    expected_map = {span: i for i, span in enumerate(expected)}

    body = _decode_b64_multipart(req.body_b64)
    raw_parts = _split_multipart_parts(body, req.boundary)

    # part 区间 -> (期望范围下标, 数据)；重复区间在解析阶段即拒绝。
    found: dict[tuple[int, int], tuple[int, bytes]] = {}
    for part_index, (header_block, payload) in enumerate(raw_parts):
        (start, end, total), content_type = _parse_part_headers(
            header_block, part_index
        )

        if total != req.object_length:
            raise MultipartParseError(
                f"Content-Range 声明的对象总长度 {total} 与请求的 "
                f"{req.object_length} 不一致",
                f"parts[{part_index}].content_range",
                part_index=part_index,
                start=start,
                end=end,
                code="object_length_mismatch",
            )
        if end < start:
            raise MultipartParseError(
                "Content-Range 的闭区间非法：end 不得小于 start",
                f"parts[{part_index}].content_range",
                part_index=part_index,
                start=start,
                end=end,
                code="invalid_content_range",
            )
        if start < 0 or end >= total:
            raise MultipartParseError(
                f"Content-Range [{start},{end}] 越界：对象总长度为 {total}",
                f"parts[{part_index}].content_range",
                part_index=part_index,
                start=start,
                end=end,
                code="part_out_of_bounds",
            )
        declared_length = end - start + 1
        if len(payload) != declared_length:
            raise MultipartParseError(
                f"part 正文 {len(payload)} 字节，与 Content-Range "
                f"[{start},{end}] 声明的 {declared_length} 字节不符",
                f"parts[{part_index}]",
                part_index=part_index,
                start=start,
                end=end,
                code="part_length_mismatch",
            )
        if req.expected_content_type is not None:
            wanted = req.expected_content_type.strip(" \t").lower()
            if content_type != wanted:
                raise MultipartParseError(
                    f"part Content-Type {content_type!r} 与期望的 "
                    f"{req.expected_content_type!r} 不一致",
                    f"parts[{part_index}].content_type",
                    part_index=part_index,
                    start=start,
                    end=end,
                    code="content_type_mismatch",
                )

        span = (start, end)
        if span in found:
            raise MultipartParseError(
                f"Content-Range [{start},{end}] 的 part 重复出现",
                f"parts[{part_index}].content_range",
                part_index=part_index,
                range_index=expected_map.get(span),
                start=start,
                end=end,
                code="duplicate_part",
            )
        if span not in expected_map:
            raise MultipartParseError(
                f"part 区间 [{start},{end}] 不在期望 ranges 中（多余段）",
                f"parts[{part_index}].content_range",
                part_index=part_index,
                start=start,
                end=end,
                code="unexpected_part",
            )
        found[span] = (expected_map[span], payload)

    missing = [
        (i, span) for i, span in enumerate(expected) if span not in found
    ]
    if missing:
        first_i, (s, e) = missing[0]
        raise MultipartParseError(
            f"缺少 {len(missing)} 个期望范围的 part："
            + ", ".join(f"[{s2},{e2}]" for _, (s2, e2) in missing),
            "body_b64",
            range_index=first_i,
            start=s,
            end=e,
            code="missing_part",
        )

    results: list[MultipartRange] = []
    for i, (start, end) in enumerate(expected):
        _, payload = found[(start, end)]
        results.append(
            MultipartRange(
                index=i,
                type=req.ranges[i].type,
                start=start,
                end=end,
                length=len(payload),
                data_b64=base64.b64encode(payload).decode("ascii"),
            )
        )

    return MultipartParseResponse(
        object_length=req.object_length,
        boundary=req.boundary,
        parts=len(raw_parts),
        ranges=results,
    )


# ---------------------------- /range-response ----------------------------

# 接受的媒体类型取值最大长度，抵御超长输入。
_MEDIA_TYPE_MAX_LENGTH = 1000
# boundary 随机片段长度（hex 后 24 字符，连同前缀仍远小于 70 字符上限）。
_BOUNDARY_RANDOM_BYTES = 12
# boundary 生成的最大尝试次数（理论上首次即可通过）。
_BOUNDARY_MAX_ATTEMPTS = 64
_BOUNDARY_PREFIX = "range-response-"


class RangeResponseRequest(BaseModel):
    """/range-response 请求：/assemble 的响应加上目标媒体类型。

    ranges 直接复用 /assemble 响应中 AssembledRange 的字段集合
    （index/type/start/end/length/data_b64），可原样回显 /assemble 的输出；
    content_type 为对象的媒体类型，将原样写入单段响应或每个 part 的
    Content-Type 头部（参数按调用方声明保留）。
    """

    object_length: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="对象总字节长度（回显 /assemble 的同名值）"
    )
    ranges: list[AssembledRange] = Field(
        ..., min_length=1, description="/assemble 返回的 ranges（原样回显）"
    )
    content_type: str = Field(
        ...,
        min_length=1,
        max_length=_MEDIA_TYPE_MAX_LENGTH,
        description="对象媒体类型，形如 type/subtype，可携带参数",
    )


class RangeResponseOut(BaseModel):
    """206 响应表示：客户端按 status_code/headers/body_b64 原样下发即可。"""

    status_code: Literal[206] = Field(206, description="HTTP 状态码，固定为 206 Partial Content")
    headers: dict[str, str] = Field(
        ..., description="响应头字段名到原始值的映射，可直接写入 HTTP 响应"
    )
    content_length: int = Field(..., description="最终二进制正文的字节数（即 Content-Length）")
    media_type: Literal["single", "multipart/byteranges"] = Field(
        ...,
        description="正文形式：single=单段范围字节，multipart/byteranges=多段分帧正文",
    )
    boundary: str | None = Field(
        None,
        description="multipart/byteranges 时实际采用的 boundary（不含前导 --）；单段为 null",
    )
    body_b64: str = Field(..., description="最终二进制响应正文的标准 Base64（含正确 = 填充）")


class RangeResponseError(Exception):
    """206 响应生成阶段的语义校验失败，统一对应 HTTP 422。

    loc 为可定位的字段路径，例如 "ranges[2].data_b64"；range_index 在
    可归属到某个范围时给出。错误响应不回显任何范围正文或 Base64。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        range_index: int | None = None,
        code: str = "range_response_error",
    ):
        self.message = message
        self.loc = loc
        self.range_index = range_index
        self.code = code
        super().__init__(message)


@app.exception_handler(RangeResponseError)
async def range_response_error_handler(request: Request, exc: RangeResponseError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.range_index is not None:
        detail["range_index"] = exc.range_index
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


def _decode_b64_range(raw: str, loc: str, range_index: int) -> bytes:
    """严格按标准 Base64（含正确填充、validate=True）解码单个范围内容。"""
    if len(raw) % 4 != 0:
        raise RangeResponseError(
            "Base64 长度不是 4 的倍数，缺少填充或包含非法字符",
            loc,
            range_index=range_index,
            code="invalid_base64",
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RangeResponseError(
            f"Base64 解码失败：{exc}",
            loc,
            range_index=range_index,
            code="invalid_base64",
        )


def _validate_media_type(raw: str) -> str:
    """校验 content_type 是合法的 HTTP 媒体类型并返回去首尾 OWS 后的原值。

    接受 `token "/" token *( OWS ";" OWS parameter )`，参数值为 token 或
    quoted-string；拒绝空值、非 ASCII、CR/LF、非法 token、非法参数，防止
    调用方通过该字段实施响应头注入。返回时保留调用方给定的大小写与参数
    形式，仅去除首尾 OWS。
    """
    if any(ch in raw for ch in "\r\n"):
        raise RangeResponseError(
            "content_type 不允许包含 CR/LF（拒绝头部注入）",
            "content_type",
            code="invalid_content_type",
        )
    try:
        value = raw.encode("ascii")
    except UnicodeEncodeError:
        raise RangeResponseError(
            "content_type 必须是 ASCII 文本",
            "content_type",
            code="invalid_content_type",
        )
    value = value.strip(b" \t")
    if not value:
        raise RangeResponseError(
            "content_type 不能为空",
            "content_type",
            code="invalid_content_type",
        )
    segments = value.split(b";")
    main_type = segments[0].strip(b" \t")
    parts = main_type.split(b"/")
    if (
        len(parts) != 2
        or not _TOKEN_RE.fullmatch(parts[0])
        or not _TOKEN_RE.fullmatch(parts[1])
    ):
        raise RangeResponseError(
            "content_type 必须是合法的 type/subtype（RFC 9110 媒体类型）",
            "content_type",
            code="invalid_content_type",
        )
    for seg in segments[1:]:
        param = seg.strip(b" \t")
        if not param or b"=" not in param:
            raise RangeResponseError(
                "content_type 参数段非法（每个参数必须形如 name=value）",
                "content_type",
                code="invalid_content_type",
            )
        name, _, pvalue = param.partition(b"=")
        name = name.strip(b" \t")
        pvalue = pvalue.strip(b" \t")
        if not _TOKEN_RE.fullmatch(name):
            raise RangeResponseError(
                "content_type 参数名不是合法 token",
                "content_type",
                code="invalid_content_type",
            )
        if pvalue.startswith(b'"') and pvalue.endswith(b'"'):
            if len(pvalue) < 2 or not _QUOTED_VALUE_RE.fullmatch(pvalue[1:-1]):
                raise RangeResponseError(
                    "content_type 参数 quoted-string 非法",
                    "content_type",
                    code="invalid_content_type",
                )
        elif not _TOKEN_RE.fullmatch(pvalue):
            raise RangeResponseError(
                "content_type 参数值既不是合法 token 也不是 quoted-string",
                "content_type",
                code="invalid_content_type",
            )
    return value.decode("ascii")


def _choose_boundary(chunks: list[bytes]) -> str:
    """随机选择一个不会与 multipart 正文分隔行冲突的 boundary。

    multipart 分帧依赖正文中出现的开边界 ``--boundary`` 与段间的
    CRLF ``--boundary``；此处先按各段正文做快速预筛（任何正文都不得
    包含 boundary 字节序列），最终完整正文的边界计数在端点中复核兜底。
    """
    for _ in range(_BOUNDARY_MAX_ATTEMPTS):
        candidate = _BOUNDARY_PREFIX + secrets.token_hex(_BOUNDARY_RANDOM_BYTES)
        delim = b"--" + candidate.encode("ascii")
        if not any(delim in payload for payload in chunks):
            return candidate
    raise RuntimeError("无法生成不与正文冲突的 multipart boundary")


def _build_multipart_body(
    chunks: list[bytes],
    ordered: list[AssembledRange],
    object_length: int,
    content_type: str,
    boundary: str,
) -> bytes:
    """按 ranges 下标顺序构造严格 CRLF 分帧的 multipart/byteranges 正文。"""
    delim = b"--" + boundary.encode("ascii")
    parts: list[bytes] = []
    for rng, payload in zip(ordered, chunks):
        parts.append(delim + _CRLF)
        parts.append(b"Content-Type: " + content_type.encode("ascii") + _CRLF)
        parts.append(
            f"Content-Range: bytes {rng.start}-{rng.end}/{object_length}".encode("ascii")
            + _CRLF + _CRLF
        )
        parts.append(payload + _CRLF)
    parts.append(delim + b"--")
    return b"".join(parts)


@app.post("/range-response", response_model=RangeResponseOut)
def range_response(req: RangeResponseRequest) -> RangeResponseOut:
    """把 /assemble 的重组结果生成为可直接下发的 206 Partial Content 表示。

    单段：正文即范围字节，响应头含 Content-Type、Content-Length 与
    Content-Range；多段：按 ranges 下标顺序生成 multipart/byteranges，
    每段附 Content-Type 与 Content-Range，Content-Length 为最终二进制
    正文长度，boundary 随机生成且保证不与任何正文分隔行冲突。
    """
    # 0. content_type 先做独立的语法校验（与具体范围无关，杜绝头注入）。
    content_type = _validate_media_type(req.content_type)

    # 1. 校验 ranges 下标：非负、不重复且恰好覆盖 0..N-1。
    index_pos: dict[int, int] = {}
    for pos, rng in enumerate(req.ranges):
        if rng.index < 0:
            raise RangeResponseError(
                "范围下标不得为负",
                f"ranges[{pos}].index",
                range_index=rng.index,
                code="invalid_range_index",
            )
        if rng.index in index_pos:
            raise RangeResponseError(
                f"范围下标 {rng.index} 在 ranges 中重复",
                f"ranges[{pos}].index",
                range_index=rng.index,
                code="duplicate_range_index",
            )
        index_pos[rng.index] = pos

    n = len(req.ranges)
    if set(index_pos) != set(range(n)):
        extra = sorted(i for i in index_pos if i >= n)
        missing_idx = sorted(set(range(n)) - set(index_pos))
        detail = f"ranges 下标应为 0..{n - 1} 的全排列"
        if missing_idx:
            detail += f"，缺少下标 {missing_idx}"
        if extra:
            detail += f"，存在多余下标 {extra}"
        raise RangeResponseError(detail, "ranges", code="invalid_range_index")

    # 2. 逐段校验区间在对象内、声明长度自洽。
    for rng in req.ranges:
        pos = index_pos[rng.index]
        if rng.end < rng.start:
            raise RangeResponseError(
                f"范围 [{rng.start},{rng.end}] 非法：end 不得小于 start",
                f"ranges[{pos}]",
                range_index=rng.index,
                code="invalid_range",
            )
        if rng.start < 0 or rng.end >= req.object_length:
            raise RangeResponseError(
                f"范围 [{rng.start},{rng.end}] 越界：对象长度为 {req.object_length}",
                f"ranges[{pos}]",
                range_index=rng.index,
                code="range_out_of_bounds",
            )
        declared_length = rng.end - rng.start + 1
        if rng.length != declared_length:
            raise RangeResponseError(
                f"范围 {rng.index} 声明长度 {rng.length} 与区间 "
                f"[{rng.start},{rng.end}] 应有的 {declared_length} 字节不符",
                f"ranges[{pos}].length",
                range_index=rng.index,
                code="range_length_mismatch",
            )

    # 3. 逐段解码 Base64，解码字节数必须等于声明长度。
    ordered = sorted(req.ranges, key=lambda r: r.index)
    chunks: list[bytes] = []
    for rng in ordered:
        pos = index_pos[rng.index]
        data = _decode_b64_range(
            rng.data_b64, f"ranges[{pos}].data_b64", rng.index
        )
        if len(data) != rng.length:
            raise RangeResponseError(
                f"范围 {rng.index} 的内容解码后 {len(data)} 字节，"
                f"与声明长度 {rng.length} 不符",
                f"ranges[{pos}].data_b64",
                range_index=rng.index,
                code="body_length_mismatch",
            )
        chunks.append(data)

    # 4. 生成最终二进制正文并按其真实字节数计算 Content-Length。
    if n == 1:
        rng = ordered[0]
        body = chunks[0]
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes {rng.start}-{rng.end}/{req.object_length}",
        }
        return RangeResponseOut(
            status_code=206,
            headers=headers,
            content_length=len(body),
            media_type="single",
            boundary=None,
            body_b64=base64.b64encode(body).decode("ascii"),
        )

    # 多段：选 boundary（预筛）→ 拼正文 → 复核边界出现次数恰好为
    # 段数 + 1（开边界 + 每段前的 CRLF 边界，末段为结束边界），保证
    # 生成的正文不会被自身的 boundary 行歧义截断。
    for _ in range(_BOUNDARY_MAX_ATTEMPTS):
        boundary = _choose_boundary(chunks)
        body = _build_multipart_body(
            chunks, ordered, req.object_length, content_type, boundary
        )
        delim = b"--" + boundary.encode("ascii")
        count = 0
        search_from = 0
        while True:
            at = body.find(delim, search_from)
            if at == -1:
                break
            count += 1
            search_from = at + len(delim)
        if count == n + 1:
            break
    else:  # pragma: no cover - 随机冲突连续失败在概率上不可达
        raise RuntimeError("无法生成不与正文冲突的 multipart boundary")

    headers = {
        "Content-Type": f"multipart/byteranges; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    return RangeResponseOut(
        status_code=206,
        headers=headers,
        content_length=len(body),
        media_type="multipart/byteranges",
        boundary=boundary,
        body_b64=base64.b64encode(body).decode("ascii"),
    )


# ----------------------------- /secure-request -----------------------------


def canonical_signing_object(
    operation: str,
    payload: dict[str, Any],
    key_id: str,
    timestamp: int,
    nonce: str,
) -> dict[str, Any]:
    """构造固定字段集合的签名对象（不含 signature 本身）。"""
    return {
        "operation": operation,
        "payload": payload,
        "key_id": key_id,
        "timestamp": timestamp,
        "nonce": nonce,
    }


def canonical_json(obj: Any) -> bytes:
    """固定 JSON 规范化：键按字典序排序、无多余空白、UTF-8、ensure_ascii=False。

    签名双方必须使用完全一致的规范化结果。不支持 NaN/Infinity
    （Python 的 json 默认 allow_nan 会输出非标准 JSON，这里显式拒绝）。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _auth_error(
    code: str, message: str, *, status_code: int, **extra: Any
) -> JSONResponse:
    """构造可定位的 401/409/503 鉴权错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message, **extra},
    )


@app.post("/secure-request")
def secure_request(raw: SecureRequest) -> JSONResponse:
    """统一鉴权入口：验签 + 时间窗 + nonce 防重放后执行 plan/assemble。

    成功时原样返回对应操作的响应（HTTP 200）。鉴权失败返回 401，
    nonce 重放返回 409，payload 语义错误沿用对应操作的 422 结构。
    """
    # 0. 服务端未配置任何密钥：明确报错，而不是静默放行。
    if not secure_config.keys:
        return _auth_error(
            "keys_not_configured",
            "服务端未配置 SECURE_REQUEST_KEYS，/secure-request 不可用",
            status_code=503,
        )

    # 1. 时间偏差校验（先于密钥查找/验签，过期请求一律拒绝）。
    now = time.time()
    skew = now - raw.timestamp
    if abs(skew) > secure_config.timestamp_skew:
        return _auth_error(
            "timestamp_skew",
            f"时间戳偏差 {skew:+.0f}s 超出允许范围 "
            f"±{secure_config.timestamp_skew}s",
            status_code=401,
            server_time=int(now),
            max_skew_seconds=secure_config.timestamp_skew,
        )

    # 2. key_id 未知：明确报错（不与签名失败混淆，便于客户端定位）。
    secret = secure_config.keys.get(raw.key_id)
    if secret is None:
        return _auth_error(
            "unknown_key_id",
            f"未知的 key_id：{raw.key_id!r}",
            status_code=401,
            key_id=raw.key_id,
        )

    # 3. 固定 JSON 规范化后重算 HMAC-SHA256，常量时间比较。
    try:
        signing_bytes = canonical_json(
            canonical_signing_object(
                raw.operation, raw.payload, raw.key_id, raw.timestamp, raw.nonce
            )
        )
    except (TypeError, ValueError):
        return _auth_error(
            "invalid_signing_payload",
            "payload 无法规范化为 JSON（包含 NaN/Infinity 或不可序列化值）",
            status_code=422,
        )
    expected = hmac.new(secret, signing_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, raw.signature.lower()):
        return _auth_error(
            "invalid_signature",
            "签名校验失败：signature 与规范化签名串的 HMAC-SHA256 不符",
            status_code=401,
        )

    # 4. nonce 防重放：同一 key_id+nonce 在有效窗口内只能成功一次。
    #    在同一把锁内完成“清理过期项 → 识别重放 → 为新 nonce 预留名额”，
    #    避免清理/判重/占位之间出现并发窗口。先占位；若后续 payload 校验或
    #    操作执行失败则回滚，避免失败请求白白消耗 nonce（只有真正成功的
    #    请求才永久占用）。容量对进行中占位与窗口内已成功 nonce 统一计数，
    #    已满且无过期项时拒绝执行（503），绝不驱逐任何未过期 nonce；
    #    409 重放优先于 503 容量不足。
    nonce_key = (raw.key_id, raw.nonce)
    with _nonce_lock:
        status, reservation = _reserve_nonce(nonce_key, now)
        if status == "replayed":
            return _auth_error(
                "nonce_replayed",
                f"key_id={raw.key_id!r} 的 nonce 已在有效窗口内使用过",
                status_code=409,
                key_id=raw.key_id,
            )
        if status == "full":
            return _auth_error(
                "nonce_capacity_exceeded",
                "nonce 防重放表已达容量上限且没有可清理的过期 nonce，"
                "请稍后重试",
                status_code=503,
                capacity=secure_config.nonce_capacity,
            )
        assert reservation is not None

    try:
        # 5. 鉴权通过：用 payload 校验对应操作的模型（失败返回带 payload
        #    路径的 422），再执行原有逻辑并原样序列化其响应。
        try:
            if raw.operation == "plan":
                result = plan(PlanRequest.model_validate(raw.payload))
            else:
                result = assemble(AssembleRequest.model_validate(raw.payload))
        except ValidationError as exc:
            errors = exc.errors()
            for err in errors:
                err["loc"] = ("payload",) + tuple(err.get("loc", ()))
            response = JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_request",
                    "detail": jsonable_encoder(
                        errors, custom_encoder={Exception: str}
                    ),
                },
            )
        else:
            response = JSONResponse(
                status_code=200, content=jsonable_encoder(result)
            )
    except BaseException:
        # 业务异常（PlanningError/AssemblyError 等 422）或其他异常：
        # 本次请求未成功，释放自己的 nonce 占位（记录已被并发请求重新
        # 占用时不动），允许客户端修正后用同一 nonce 重试。
        with _nonce_lock:
            _release_nonce(nonce_key, reservation)
        raise

    # payload 校验失败（未执行任何操作）：同样回滚自己的 nonce 占位。
    if response.status_code != 200:
        with _nonce_lock:
            _release_nonce(nonce_key, reservation)
    return response


# --------------------------- /safe-download-name ---------------------------

# RFC 7230 token（tchar），字节级匹配。
_DISPO_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# RFC 5987 attr-char（value 中允许的非百分号字节）。
_ATTR_CHAR_RE = re.compile(rb"^[!#$&+.^_`|~0-9A-Za-z-]+$")
# filename* 取值的 charset（大小写不敏感）：仅接受 UTF-8 与 ISO-8859-1。
_EXTENDED_CHARSETS = frozenset({"utf-8", "iso-8859-1"})
# RFC 5987 language-tag：1*8ALPHA *( "-" 1*8alphanum )，可缺省。
_LANGUAGE_TAG_RE = re.compile(r"^(?:[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*)?$")

# 文件名清理时的路径分隔符：同时覆盖 POSIX("/") 与 Windows("\\") 风格。
_PATH_SEPARATORS = re.compile(r"[\\/]")
# C0 控制字符（0x00-0x1F）与 DEL（0x7F）；NUL 等也一并去除。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Windows 会忽略尾部的点与空格；循环去除直至稳定，避免 "a . . " 之类变体。
_TRAILING_DOT_SPACE_RE = re.compile(r"[ .]+$")
# 仅由点构成的名称（""、"."、".."、"..." 等）一律视为危险名称。
_ALL_DOTS_RE = re.compile(r"^\.+$")
# 接受的 Content-Disposition 头值最大长度，抵御超长输入。
_DOWNLOAD_HEADER_MAX_LENGTH = 8192


class SafeDownloadNameRequest(BaseModel):
    """解析上游 Content-Disposition 的请求。

    content_disposition 为上游响应头 Content-Disposition 的原始值
    （HTTP 头按 ISO-8859-1 在线传输；若调用方已按其他字符集解码为
    str，服务会先尝试 ISO-8859-1 还原为字节，失败时按 UTF-8 还原）。
    """

    content_disposition: str = Field(
        ...,
        min_length=1,
        max_length=_DOWNLOAD_HEADER_MAX_LENGTH,
        description="上游 Content-Disposition 响应头的原始值",
    )


class SafeDownloadNameResponse(BaseModel):
    filename: str = Field(..., description="去除路径前缀与危险字符后可安全使用的单一文件名")
    source: Literal["filename*", "filename"] = Field(
        ..., description="最终采用的参数来源：RFC 5987 的 filename* 或回退的 filename"
    )


class DownloadNameError(Exception):
    """Content-Disposition 无法解析出安全文件名，统一对应 HTTP 422。"""

    def __init__(self, message: str, code: str = "invalid_content_disposition"):
        self.message = message
        self.code = code
        super().__init__(message)


@app.exception_handler(DownloadNameError)
async def download_name_error_handler(request: Request, exc: DownloadNameError):
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "message": exc.message},
    )


def _header_to_bytes(raw: str) -> bytes:
    """把（可能被代理按 UTF-8 解码过的）头值还原为线上原始字节。

    HTTP 头的线上编码是 ISO-8859-1；调用方若直接传入 latin-1 解码的字符串，
    这里可逆还原为字节。若字符串含 U+00FF 以上字符（说明曾被按 UTF-8 解码），
    则按 UTF-8 还原，保证 RFC 5987 的百分号字节与 quoted-string 内的 UTF-8
    原始字节都能被正确解析。
    """
    try:
        return raw.encode("iso-8859-1")
    except UnicodeEncodeError:
        return raw.encode("utf-8")


def _parse_disposition_params(value: bytes) -> tuple[bytes, list[tuple[str, bytes]]]:
    """解析 `disposition-type *( OWS ";" OWS parameter )`，返回 (type, 参数列表)。

    参数顺序按出现顺序保留；语法结构错误（缺分号、参数名非 token、缺 "="、
    值非 token/quoted-string、引号未闭合、quoted-pair 转义非法等）抛出
    DownloadNameError。参数名按 ASCII 小写返回，值为词法还原后的字节
    （quoted-string 已去引号并解析反斜杠转义）。
    """
    n = len(value)
    pos = 0

    def skip_ows(i: int) -> int:
        while i < n and value[i : i + 1] in (b" ", b"\t"):
            i += 1
        return i

    pos = skip_ows(pos)
    start = pos
    while pos < n and value[pos : pos + 1] not in (b";", b" ", b"\t"):
        pos += 1
    dtype = value[start:pos]
    pos = skip_ows(pos)
    if not _DISPO_TOKEN_RE.fullmatch(dtype):
        raise DownloadNameError(
            "Content-Disposition 的 disposition-type 缺失或不是合法 token"
        )

    params: list[tuple[str, bytes]] = []
    while pos < n:
        if value[pos : pos + 1] != b";":
            raise DownloadNameError(
                "Content-Disposition 参数之间必须以 ';' 分隔（发现非法字符）"
            )
        pos += 1
        pos = skip_ows(pos)

        # 参数名：token。
        name_start = pos
        while pos < n and value[pos : pos + 1] not in (b"=", b" ", b"\t", b";"):
            pos += 1
        name = value[name_start:pos]
        pos = skip_ows(pos)
        if not name or pos >= n or value[pos : pos + 1] != b"=":
            raise DownloadNameError(
                f"Content-Disposition 参数 {name.decode('ascii', 'replace')!r} 缺少 '='"
            )
        if not _DISPO_TOKEN_RE.fullmatch(name):
            raise DownloadNameError(
                f"Content-Disposition 参数名 {name.decode('ascii', 'replace')!r} 不是合法 token"
            )
        pos += 1
        pos = skip_ows(pos)
        if pos >= n:
            raise DownloadNameError(
                f"Content-Disposition 参数 {name.decode('ascii', 'replace')!r} 缺少取值"
            )

        # 参数值：token 或 quoted-string。
        if value[pos : pos + 1] == b'"':
            pos += 1
            out = bytearray()
            while True:
                if pos >= n:
                    raise DownloadNameError(
                        "Content-Disposition 的 quoted-string 未闭合（缺少结尾 '\"'）"
                    )
                ch = value[pos]
                if ch == 0x22:  # 引号收尾
                    pos += 1
                    break
                if ch in (0x0D, 0x0A):
                    raise DownloadNameError(
                        "Content-Disposition 参数值不允许包含 CR/LF（拒绝折行与头部注入）"
                    )
                if ch == 0x5C:  # 反斜杠 quoted-pair
                    pos += 1
                    if pos >= n:
                        raise DownloadNameError(
                            "Content-Disposition 的 quoted-pair 转义被截断"
                        )
                    esc = value[pos]
                    if not (0x20 <= esc <= 0x7E):
                        raise DownloadNameError(
                            "Content-Disposition 的 quoted-pair 只能转义可见 ASCII 字符"
                        )
                    out.append(esc)
                    pos += 1
                else:
                    # qdtext：除 CR/LF 外的字节均保留。tab(0x09)、SP 及可见
                    # ASCII 是 RFC 7230 允许的 qdtext；现实中大量上游直接把
                    # UTF-8 等非 ASCII 字节（obs-text，0x80-0xFF）放进
                    # quoted-string，原样保留交由解码阶段按 UTF-8/ISO-8859-1
                    # 解释。NUL、BEL、DEL 等控制字符也原样保留，由清理阶段
                    # 统一去除；只有 CR/LF 在此拒绝（折行/头部注入）。
                    out.append(ch)
                    pos += 1
            pvalue = bytes(out)
        else:
            val_start = pos
            while pos < n and value[pos : pos + 1] not in (b";", b" ", b"\t"):
                pos += 1
            pvalue = value[val_start:pos]
            if not _DISPO_TOKEN_RE.fullmatch(pvalue):
                raise DownloadNameError(
                    "Content-Disposition 参数值既不是合法 token 也不是 quoted-string"
                )

        pos = skip_ows(pos)
        params.append((name.decode("ascii").lower(), pvalue))

    return bytes(dtype).lower(), params


def _decode_extended_value(raw: bytes) -> str:
    """严格按 RFC 5987 解码 filename* 的 ``charset'language'value``。

    - 三段必须恰好以两个 ASCII 单引号分隔；
    - charset 仅接受 UTF-8 与 ISO-8859-1（大小写不敏感）；
    - language 必须匹配 language-tag 语法（可缺省）；
    - value 由 attr-char 与百分号字节组成，百分号后必须紧跟两位十六进制数；
    - 百分号还原后的字节必须能按 charset 成功解码（UTF-8 拒绝非法序列）。
    """
    parts = raw.split(b"'")
    if len(parts) != 3:
        raise DownloadNameError(
            "filename* 必须形如 charset'language'value（单引号数量不为 2）"
        )
    charset_b, language_b, value_b = parts
    try:
        charset = charset_b.decode("ascii").lower()
    except UnicodeDecodeError:
        raise DownloadNameError("filename* 的 charset 不是 ASCII token")
    if charset not in _EXTENDED_CHARSETS:
        raise DownloadNameError(
            f"filename* 的 charset {charset!r} 不受支持（仅接受 UTF-8 与 ISO-8859-1）"
        )
    try:
        language = language_b.decode("ascii")
    except UnicodeDecodeError:
        raise DownloadNameError("filename* 的 language 不是 ASCII")
    if not _LANGUAGE_TAG_RE.fullmatch(language):
        raise DownloadNameError(f"filename* 的 language {language!r} 不符合 language-tag 语法")

    out = bytearray()
    i = 0
    while i < len(value_b):
        if value_b[i] == 0x25:  # '%'
            hexpart = value_b[i + 1 : i + 3]
            if not re.fullmatch(rb"[0-9A-Fa-f]{2}", hexpart):
                raise DownloadNameError("filename* 的百分号转义后必须紧跟两位十六进制数")
            out.append(int(hexpart, 16))
            i += 3
            continue
        # 一段连续的 attr-char（不含 '%'）一次性校验。
        j = i
        while j < len(value_b) and value_b[j] != 0x25:
            j += 1
        segment = value_b[i:j]
        if not _ATTR_CHAR_RE.fullmatch(segment):
            raise DownloadNameError("filename* 的 value 含 attr-char 之外的原始字节")
        out.extend(segment)
        i = j

    try:
        return bytes(out).decode(charset)
    except UnicodeDecodeError:
        raise DownloadNameError(f"filename* 的字节序列无法按 {charset.upper()} 解码")


def _decode_legacy_value(raw: bytes) -> str:
    """解码传统 filename 的 token / quoted-string 取值。

    线上头值以 ISO-8859-1 传输，但实践中大量上游直接放入 UTF-8 字节
    （尤其 quoted-string 内的中文文件名）。优先按 UTF-8 严格解码，
    失败时回落到 ISO-8859-1（逐字节可逆，永不失败），与浏览器常见行为一致。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("iso-8859-1")


def sanitize_download_name(decoded: str) -> str:
    """把解码后的参数值清理为单一、安全的文件名；无有效名称时返回空串。

    步骤：去除全部控制字符（含 NUL/DEL）→ 去除首尾空白 → 按 ``/`` 与
    ``\\`` 取最后一段（剥离任意路径前缀）→ 循环剥离尾部点/空格（Windows
    会忽略它们，可用于歧义绕过）→ 拒绝空名与仅由点构成的名称。
    """
    cleaned = _CONTROL_CHARS_RE.sub("", decoded).strip()
    base = _PATH_SEPARATORS.split(cleaned)[-1].strip()
    while True:
        newer = _TRAILING_DOT_SPACE_RE.sub("", base).strip()
        if newer == base:
            break
        base = newer
    if not base or _ALL_DOTS_RE.fullmatch(base):
        return ""
    return base


def derive_download_name(raw: str) -> tuple[str, str]:
    """解析原始 Content-Disposition 头值，返回 (安全文件名, 来源)。

    按参数出现顺序选择：第一个语法合法且清理后非空的 `filename*`
    （RFC 5987）一旦出现即优先采用；`filename*` 缺失或无效时，回退到第一个
    清理后非空的 `filename`。同名参数重复时只看第一次出现（后续重复忽略）。
    两者都无法得到有效名称时抛 DownloadNameError（HTTP 422）。
    """
    if any(ch in raw for ch in "\r\n"):
        raise DownloadNameError(
            "Content-Disposition 头值不允许包含 CR/LF（拒绝折行与头部注入）"
        )
    _, params = _parse_disposition_params(_header_to_bytes(raw))

    extended: str | None = None  # 首个有效 filename*
    extended_seen = False  # 是否出现过 filename*（即使首个非法也不再看后续重复项）
    legacy: str | None = None  # 首个有效 filename（回退用）
    for name, pvalue in params:
        if name == "filename*":
            if extended_seen:
                continue  # 重复参数：只认第一次出现
            extended_seen = True
            try:
                extended = sanitize_download_name(_decode_extended_value(pvalue)) or None
            except DownloadNameError:
                extended = None
        elif name == "filename" and legacy is None:
            # 重复参数只取第一个；清理后为空则视为无效。
            legacy = sanitize_download_name(_decode_legacy_value(pvalue)) or None

    if extended is not None:
        return extended, "filename*"
    if legacy is not None:
        return legacy, "filename"
    raise DownloadNameError(
        "Content-Disposition 中未提供有效的 filename* 或 filename，"
        "无法得到可安全使用的下载文件名",
        code="no_safe_filename",
    )


@app.post("/safe-download-name", response_model=SafeDownloadNameResponse)
def safe_download_name(req: SafeDownloadNameRequest) -> SafeDownloadNameResponse:
    """解析上游 Content-Disposition，返回去除路径与危险字符后的单一文件名。"""
    filename, source = derive_download_name(req.content_disposition)
    return SafeDownloadNameResponse(filename=filename, source=source)


# --------------------------- /client-range-decision ---------------------------

# 头值长度上限：抵御超长输入（HTTP 头本身通常远小于此）。
_CLIENT_HEADER_MAX_LENGTH = 8192

# IMF-fixdate（RFC 9110 §5.6.7）：Day, DD Mon YYYY HH:MM:SS GMT。
_IMF_FIXDATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) "
    r"(\d{2}):(\d{2}):(\d{2}) GMT$"
)
_IMF_WEEKDAYS: dict[str, int] = {
    "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6,
}
_IMF_MONTHS: dict[str, int] = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_IMF_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_IMF_MONTH_NAMES = (
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _format_imf_fixdate(dt: datetime.datetime) -> str:
    """把 UTC datetime 格式化为 IMF-fixdate（不依赖运行环境 locale）。"""
    return (
        f"{_IMF_WEEKDAY_NAMES[dt.weekday()]}, "
        f"{dt.day:02d} {_IMF_MONTH_NAMES[dt.month]} {dt.year:04d} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} GMT"
    )
# 头部解析共用：CR/LF 及其他控制字符一律拒绝（防注入与折行）。
_OWS_STRIP = " \t"


class ClientRangeDecisionRequest(BaseModel):
    """/client-range-decision 请求：客户端条件头 + 对象元数据 + /plan 参数。

    range_header 省略或为 null 表示客户端未发送 Range（决策为完整对象
    200）；etag 与 last_modified 至少提供一个，用于评估 If-Range。
    """

    object_length: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="对象总字节长度"
    )
    etag: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="对象当前的强 ETag 头原值（如 '\"v3\"'）；不得为弱标签",
    )
    last_modified: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="对象当前的 Last-Modified 头原值（IMF-fixdate）",
    )
    range_header: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="客户端 Range 头原值（仅支持 bytes 单位）；省略/null 视为无 Range",
    )
    if_range: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="客户端 If-Range 头原值：强 ETag 或 IMF-fixdate",
    )
    max_gap: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="允许合并的最大间隙（字节），透传给 /plan"
    )
    fetch_limit: Annotated[int, StrictInt] = Field(
        ..., ge=1, description="单次回源上限（字节），透传给 /plan"
    )
    cached_blocks: list[CachedBlock] = Field(
        default_factory=list,
        description="可选的已缓存块列表；语义与 /plan 完全一致",
    )

    @model_validator(mode="after")
    def _check_validators_and_cache_ids(self) -> "ClientRangeDecisionRequest":
        # 显式空字符串由各自解析器按非法头值报 422（loc 可定位）；
        # 这里只保证至少提供一个验证器字段。
        if self.etag is None and self.last_modified is None:
            raise ValueError("etag 与 last_modified 至少必须提供一个")
        seen: set[object] = set()
        for cb in self.cached_blocks:
            if cb.cache_id in seen:
                raise ValueError(
                    f"cached_blocks 中 cache_id {cb.cache_id!r} 重复"
                )
            seen.add(cb.cache_id)
        return self


class ClientRangeSegment(BaseModel):
    """单个 Range 段的解析与规范化结果（含被跳过的不可满足段）。"""

    index: int = Field(..., description="该段在 Range 头中的顺序下标（从 0 起，忽略空成员）")
    kind: Literal["closed", "open", "suffix"] = Field(
        ..., description="closed=bytes=A-B，open=bytes=A-，suffix=bytes=-N"
    )
    requested_start: int | None = Field(
        ..., description="closed/open 的首字节位置；suffix 为 null"
    )
    requested_end: int | None = Field(
        ..., description="closed 的末字节位置；open/suffix 为 null"
    )
    suffix_length: int | None = Field(
        ..., description="suffix 请求的末尾字节数；closed/open 为 null"
    )
    satisfiable: bool = Field(..., description="是否可满足（不可满足段会被跳过）")
    start: int | None = Field(
        None, description="规范化后的起始字节偏移（含）；不可满足时为 null"
    )
    end: int | None = Field(
        None, description="规范化后的结束字节偏移（含）；不可满足时为 null"
    )
    plan_range_index: int | None = Field(
        None,
        description="送入 /plan 的范围下标（仅可满足段，按可满足段顺序从 0 起）",
    )
    reason: str = Field(
        ...,
        description="satisfied / skipped_start_past_end（闭区间/开放终点起点 >= 总长）"
        " / skipped_empty_object_suffix（空对象上的 suffix）",
    )


class ClientRangeDecision(BaseModel):
    """代理可直接执行的决策：status_code + headers + 206 时的 /plan 结果。"""

    status_code: Literal[200, 206, 416] = Field(
        ..., description="应对客户端使用的 HTTP 状态码"
    )
    reason: str = Field(
        ...,
        description="决策原因：range_absent / if_range_mismatch / partial / unsatisfiable",
    )
    headers: dict[str, str] = Field(
        ..., description="应随响应下发的头字段（206 单段或 416 时含 Content-Range）"
    )
    object_length: int
    media_type: Literal["single", "multipart/byteranges"] | None = Field(
        None,
        description="206 时的正文形式：1 段为 single，多段为 multipart/byteranges；"
        "200/416 为 null",
    )
    content_length: int | None = Field(
        None,
        description="200 时为完整对象长度；206 时为可满足范围字节数之和"
        "（多段 multipart 不含分帧开销，实际 Content-Length 以 /range-response "
        "生成的正文为准）；416 为 null",
    )
    segments: list[ClientRangeSegment] = Field(
        default_factory=list,
        description="Range 头各段解析结果；无 Range（200）时为空列表",
    )
    satisfied_count: int = Field(
        ..., description="可满足段数量（206 时 >=1，416 时为 0）"
    )
    skipped_count: int = Field(..., description="被跳过的不可满足段数量")
    if_range_matched: bool = Field(
        ..., description="If-Range 是否与当前验证器匹配（未携带 If-Range 时为 true）"
    )
    if_range_kind: Literal["etag", "date"] | None = Field(
        None, description="If-Range 的解释方式；未携带时为 null"
    )
    plan: PlanResponse | None = Field(
        None,
        description="206 决策的 /plan 完整结果：blocks/mappings/cached_blocks "
        "可原样用于回源与 /assemble；200/416 为 null",
    )


class ClientRangeError(Exception):
    """客户端头（Range/If-Range/ETag/Last-Modified）解析与校验失败，HTTP 422。

    loc 为可定位的字段路径："range_header"、"range_header.ranges[i]"、
    "if_range"、"etag"、"last_modified"。
    """

    def __init__(
        self,
        message: str,
        loc: str,
        *,
        code: str = "client_range_error",
        range_index: int | None = None,
    ):
        self.message = message
        self.loc = loc
        self.code = code
        self.range_index = range_index
        super().__init__(message)


@app.exception_handler(ClientRangeError)
async def client_range_error_handler(request: Request, exc: ClientRangeError):
    detail: dict[str, object] = {"message": exc.message, "loc": exc.loc}
    if exc.range_index is not None:
        detail["range_index"] = exc.range_index
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


def _reject_header_chars(value: str, loc: str) -> None:
    """拒绝头值中的 CR/LF 与其他 ASCII 控制字符（防折行/头部注入）。"""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ClientRangeError(
            "头值不允许包含 CR/LF 或其他控制字符（拒绝折行与头部注入）",
            loc,
            code="malformed_header",
        )


def _parse_current_strong_etag(raw: str) -> str:
    """校验对象当前 ETag 必须是合法强 ETag，返回去首尾 OWS 后的原值。"""
    _reject_header_chars(raw, "etag")
    value = raw.strip(_OWS_STRIP)
    if not value:
        raise ClientRangeError("etag 不能为空", "etag", code="missing_etag")
    if value[:2].lower() == "w/":
        raise ClientRangeError(
            "对象当前 ETag 是弱 ETag（W/ 前缀）：If-Range 只允许强比较，"
            "弱验证器不得作为当前验证器",
            "etag",
            code="weak_current_etag",
        )
    if not _STRONG_ETAG_RE.fullmatch(value):
        raise ClientRangeError(
            "对象当前 ETag 不是合法的强 ETag：必须为双引号包裹的 "
            'opaque-tag（如 "v1"），不得含空格、控制字符或裸引号',
            "etag",
            code="invalid_current_etag",
        )
    return value


def _parse_http_date(raw: str, loc: str, *, code: str) -> datetime.datetime:
    """严格解析 IMF-fixdate（RFC 9110 §5.6.7），返回 UTC 感知 datetime。

    大小写敏感、星期必须与日历日期一致、拒绝越界的日/时/分/秒；不接受
    RFC 850 / asctime 等废弃日期形式。
    """
    _reject_header_chars(raw, loc)
    m = _IMF_FIXDATE_RE.fullmatch(raw.strip(_OWS_STRIP))
    if m is None:
        raise ClientRangeError(
            "不是合法的 IMF-fixdate，期望形如 'Wed, 21 Oct 2015 07:28:00 GMT' "
            "（不接受 RFC 850/asctime 等废弃日期形式）",
            loc,
            code=code,
        )
    wd_name, day, mon_name, year, hour, minute, second = m.groups()
    try:
        dt = datetime.datetime(
            int(year),
            _IMF_MONTHS[mon_name],
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=datetime.timezone.utc,
        )
    except ValueError as exc:
        raise ClientRangeError(f"日期的日/时间分量越界：{exc}", loc, code=code)
    if dt.weekday() != _IMF_WEEKDAYS[wd_name]:
        raise ClientRangeError(
            f"星期 {wd_name} 与日历日期 {dt.date().isoformat()} 不一致",
            loc,
            code=code,
        )
    return dt


# Range 头中单个成员的解析结果：
#   ("closed", start, end) / ("open", start, None) / ("suffix", None, last_n)
def _parse_range_header(raw: str) -> list[tuple[str, int | None, int | None]]:
    """严格按 RFC 9110 §14.1.1 解析 ``bytes=...`` 字节范围头。

    仅接受 bytes 单位（大小写不敏感）；成员可以是闭区间 ``A-B``、开放终点
    ``A-`` 或 suffix ``-N``。允许成员前后及逗号周围的 OWS，但数字内部不
    得有空白；空成员（重复逗号）忽略。任何语法错误（反转区间、负数、
    非数字、空表达式等）抛 ClientRangeError(loc="range_header...")。
    返回 [(kind, a, b), ...]：closed=(closed,start,end)、
    open=(open,start,None)、suffix=(suffix,None,last_n)。
    """
    loc = "range_header"
    _reject_header_chars(raw, loc)
    text = raw.strip(_OWS_STRIP)
    if not text:
        raise ClientRangeError(
            "Range 头为空：省略/为 null 表示无 Range，空字符串是非法头值",
            loc,
            code="invalid_range_header",
        )
    # 单位与 '=' 之间不得有空白（RFC：bytes-unit = "bytes" "="）。
    if len(text) < 6 or text[:5].lower() != "bytes" or text[5] != "=":
        raise ClientRangeError(
            "Range 仅支持 bytes 单位，且必须形如 'bytes=<ranges>'",
            loc,
            code="invalid_range_unit",
        )
    body = text[6:].strip(_OWS_STRIP)
    if not body:
        raise ClientRangeError(
            "Range 头 'bytes=' 之后没有任何范围表达式",
            loc,
            code="invalid_range_header",
        )

    members: list[tuple[str, int | None, int | None]] = []
    for pos, part in enumerate(body.split(",")):
        token = part.strip(_OWS_STRIP)
        if not token:
            # 容忍重复逗号产生的空成员（RFC 允许 OWS，空集合在整体校验时报错）。
            continue
        member_loc = f"{loc}.ranges[{pos}]"
        if any(ch in token for ch in " \t"):
            raise ClientRangeError(
                f"范围表达式 {token!r} 内部不允许出现空白",
                member_loc,
                code="invalid_range_spec",
                range_index=pos,
            )
        if token.startswith("-"):
            digits = token[1:]
            if not digits or not digits.isascii() or not digits.isdigit():
                raise ClientRangeError(
                    f"suffix 范围 {token!r} 非法：必须形如 '-N'，N 为正整数",
                    member_loc,
                    code="invalid_range_spec",
                    range_index=pos,
                )
            last_n = int(digits)
            if last_n == 0:
                raise ClientRangeError(
                    "suffix 范围 '-0' 非法：suffix-byte-range-spec 的长度必须 >= 1",
                    member_loc,
                    code="invalid_range_spec",
                    range_index=pos,
                )
            members.append(("suffix", None, last_n))
            continue

        if "-" not in token:
            raise ClientRangeError(
                f"范围表达式 {token!r} 非法：闭区间需形如 'A-B'，开放终点需形如 'A-'",
                member_loc,
                code="invalid_range_spec",
                range_index=pos,
            )
        first, sep, second = token.partition("-")
        if not first.isascii() or not first.isdigit() or "-" in second:
            raise ClientRangeError(
                f"范围表达式 {token!r} 非法：数字之间出现多余 '-' 或非数字字符",
                member_loc,
                code="invalid_range_spec",
                range_index=pos,
            )
        start = int(first)
        if second == "":
            members.append(("open", start, None))
        else:
            if not second.isascii() or not second.isdigit():
                raise ClientRangeError(
                    f"闭区间 {token!r} 的末字节位置必须是非负整数",
                    member_loc,
                    code="invalid_range_spec",
                    range_index=pos,
                )
            end = int(second)
            if end < start:
                raise ClientRangeError(
                    f"闭区间 {token!r} 非法：末字节位置 {end} 小于首字节位置 {start}",
                    member_loc,
                    code="invalid_range_spec",
                    range_index=pos,
                )
            members.append(("closed", start, end))

    if not members:
        raise ClientRangeError(
            "Range 头不包含任何有效的字节范围表达式",
            loc,
            code="invalid_range_header",
        )
    return members


def _parse_if_range(
    raw: str,
) -> tuple[Literal["etag", "date"], str | datetime.datetime]:
    """解析 If-Range 头为 (kind, value)。

    以双引号开头时按 entity-tag 解析：必须是合法 opaque-tag；弱标签
    （W/ 前缀）语法合法但永远不能强匹配，由比较阶段决定；其他值按
    IMF-fixdate 解析。任何语法错误抛 ClientRangeError(loc="if_range")。
    """
    loc = "if_range"
    _reject_header_chars(raw, loc)
    value = raw.strip(_OWS_STRIP)
    if not value:
        raise ClientRangeError(
            "If-Range 头为空：省略/null 表示不携带条件，空字符串是非法头值",
            loc,
            code="invalid_if_range",
        )
    if value[:1] == '"':
        # entity-tag 形式（强标签）。
        if not _STRONG_ETAG_RE.fullmatch(value):
            raise ClientRangeError(
                "If-Range 的 ETag 不是合法的强 opaque-tag：必须为双引号包裹"
                '（如 "v1"），不得含空格、控制字符或裸引号',
                loc,
                code="invalid_if_range_etag",
            )
        return "etag", value
    if value[:2].lower() == "w/":
        # 弱标签语法合法，但 If-Range 只做强比较：交给比较阶段判定不匹配。
        weak = value[2:]
        if not _STRONG_ETAG_RE.fullmatch(weak):
            raise ClientRangeError(
                "If-Range 的弱 ETag 取值不是合法 opaque-tag",
                loc,
                code="invalid_if_range_etag",
            )
        return "etag", value
    return "date", _parse_http_date(value, loc, code="invalid_if_range_date")


def run_client_range_decision(
    req: ClientRangeDecisionRequest,
) -> ClientRangeDecision:
    """把客户端 Range/If-Range 头转换为对象代理可执行的响应决策。

    1. 校验当前验证器（强 ETag / Last-Modified）与 If-Range 头语法；
    2. 解析 Range 头（无 Range 直接给出完整对象 200 决策）；
    3. 携带 If-Range 且与当前验证器不强匹配时，忽略 Range，给出完整
       对象 200 决策（弱 ETag 永不强匹配）；
    4. 逐段规范化，跳过单个不可满足段；全部不可满足给出 416 决策
       （Content-Range: bytes */总长）；
    5. 对可满足段复用缓存感知 /plan 逻辑，给出 206 决策与可直接用于
       /assemble 的 plan。
    """
    # 0. 当前验证器：至少一个非空（pydantic 层已校验），各自必须合法。
    #    显式提供的空白字符串属于非法头值（区别于省略/null），交由解析器报错。
    current_etag: str | None = None
    if req.etag is not None:
        current_etag = _parse_current_strong_etag(req.etag)
    current_lm: datetime.datetime | None = None
    if req.last_modified is not None:
        current_lm = _parse_http_date(
            req.last_modified, "last_modified", code="invalid_last_modified"
        )

    # 1. If-Range 语法先解析（即使没有 Range，非法头值仍是 422；null/省略
    #    才是未携带，显式空白字符串为非法头值）。
    if_range_kind: Literal["etag", "date"] | None = None
    if_range_value: str | datetime.datetime | None = None
    if req.if_range is not None:
        if_range_kind, if_range_value = _parse_if_range(req.if_range)

    # 2. Range 头：显式空白值视为非法头值；省略/null 才是“无 Range”。
    if req.range_header is not None and not req.range_header.strip(_OWS_STRIP):
        raise ClientRangeError(
            "Range 头为空白：省略/null 表示无 Range，空字符串是非法头值",
            "range_header",
            code="invalid_range_header",
        )
    if req.range_header is None:
        # 无 Range：直接完整对象 200（If-Range 无意义，已在上面完成语法校验）。
        return ClientRangeDecision(
            status_code=200,
            reason="range_absent",
            headers={"Content-Length": str(req.object_length)},
            object_length=req.object_length,
            media_type=None,
            content_length=req.object_length,
            segments=[],
            satisfied_count=0,
            skipped_count=0,
            if_range_matched=True,
            if_range_kind=None,
            plan=None,
        )

    members = _parse_range_header(req.range_header)

    # 3. If-Range 强比较：ETag 全等（弱标签永不匹配）；日期采用
    #    “当前 Last-Modified 早于或等于 If-Range 时刻”的强比较规则。
    if_range_matched = True
    if if_range_kind is not None:
        if if_range_kind == "etag":
            # 弱标签（W/ 前缀）不允许强匹配；强标签与当前 ETag 逐字节全等。
            asserted = if_range_value
            assert isinstance(asserted, str)
            if_range_matched = (
                asserted[:2].lower() != "w/"
                and current_etag is not None
                and asserted == current_etag
            )
        else:
            asserted_dt = if_range_value
            assert isinstance(asserted_dt, datetime.datetime)
            if_range_matched = (
                current_lm is not None and current_lm <= asserted_dt
            )

    # 4. If-Range 不匹配：忽略 Range，返回完整对象的 200 决策。
    if not if_range_matched:
        return ClientRangeDecision(
            status_code=200,
            reason="if_range_mismatch",
            headers={"Content-Length": str(req.object_length)},
            object_length=req.object_length,
            media_type=None,
            content_length=req.object_length,
            segments=[],
            satisfied_count=0,
            skipped_count=0,
            if_range_matched=False,
            if_range_kind=if_range_kind,
            plan=None,
        )

    # 5. 逐段规范化为闭区间；单个不可满足段跳过（RFC 9110：只要还有可
    #    满足段就发送 206，不可满足段被忽略）。
    segments: list[ClientRangeSegment] = []
    specs: list[RangeSpec] = []
    plan_index = 0
    for i, (kind, a, b) in enumerate(members):
        if kind == "closed":
            start, end = a, b
            assert start is not None and end is not None
            if start >= req.object_length:
                segments.append(
                    ClientRangeSegment(
                        index=i,
                        kind="closed",
                        requested_start=start,
                        requested_end=end,
                        suffix_length=None,
                        satisfiable=False,
                        start=None,
                        end=None,
                        plan_range_index=None,
                        reason="skipped_start_past_end",
                    )
                )
                continue
            clamped_end = min(end, req.object_length - 1)
            segments.append(
                ClientRangeSegment(
                    index=i,
                    kind="closed",
                    requested_start=start,
                    requested_end=end,
                    suffix_length=None,
                    satisfiable=True,
                    start=start,
                    end=clamped_end,
                    plan_range_index=plan_index,
                    reason="satisfied",
                )
            )
            specs.append(IntervalRange(type="interval", start=start, end=clamped_end))
            plan_index += 1
        elif kind == "open":
            start = a
            assert start is not None
            if start >= req.object_length:
                segments.append(
                    ClientRangeSegment(
                        index=i,
                        kind="open",
                        requested_start=start,
                        requested_end=None,
                        suffix_length=None,
                        satisfiable=False,
                        start=None,
                        end=None,
                        plan_range_index=None,
                        reason="skipped_start_past_end",
                    )
                )
                continue
            end = req.object_length - 1
            segments.append(
                ClientRangeSegment(
                    index=i,
                    kind="open",
                    requested_start=start,
                    requested_end=None,
                    suffix_length=None,
                    satisfiable=True,
                    start=start,
                    end=end,
                    plan_range_index=plan_index,
                    reason="satisfied",
                )
            )
            specs.append(IntervalRange(type="interval", start=start, end=end))
            plan_index += 1
        else:  # suffix
            last_n = b
            assert last_n is not None
            if req.object_length == 0:
                segments.append(
                    ClientRangeSegment(
                        index=i,
                        kind="suffix",
                        requested_start=None,
                        requested_end=None,
                        suffix_length=last_n,
                        satisfiable=False,
                        start=None,
                        end=None,
                        plan_range_index=None,
                        reason="skipped_empty_object_suffix",
                    )
                )
                continue
            start = max(0, req.object_length - last_n)
            end = req.object_length - 1
            segments.append(
                ClientRangeSegment(
                    index=i,
                    kind="suffix",
                    requested_start=None,
                    requested_end=None,
                    suffix_length=last_n,
                    satisfiable=True,
                    start=start,
                    end=end,
                    plan_range_index=plan_index,
                    reason="satisfied",
                )
            )
            specs.append(SuffixRange(type="suffix", last_n=last_n))
            plan_index += 1

    satisfied = plan_index
    skipped = len(segments) - satisfied

    # 6. 全部不可满足：416，Content-Range: bytes */总长。
    if satisfied == 0:
        return ClientRangeDecision(
            status_code=416,
            reason="unsatisfiable",
            headers={"Content-Range": f"bytes */{req.object_length}"},
            object_length=req.object_length,
            media_type=None,
            content_length=None,
            segments=segments,
            satisfied_count=0,
            skipped_count=skipped,
            if_range_matched=True,
            if_range_kind=if_range_kind,
            plan=None,
        )

    # 7. 对可满足段复用现有缓存感知规划逻辑。客户端范围可能整段超过
    #    fetch_limit（HTTP 允许请求整个对象），因此统一走可拆块的缓存
    #    感知路径；无 cached_blocks 时其切片来源全部为 fetch，结果
    #    （blocks/mappings/cached_blocks）与 /plan 输出结构完全一致，
    #    可直接回交 /assemble。
    plan_req = PlanRequest(
        object_length=req.object_length,
        ranges=specs,
        max_gap=req.max_gap,
        fetch_limit=req.fetch_limit,
        cached_blocks=req.cached_blocks,
    )
    plan_result = plan_with_cache(plan_req)

    total_bytes = sum(seg.end - seg.start + 1 for seg in segments if seg.satisfiable)
    headers: dict[str, str]
    if satisfied == 1:
        only = next(seg for seg in segments if seg.satisfiable)
        assert only.start is not None and only.end is not None
        media_type: Literal["single", "multipart/byteranges"] = "single"
        # 单段：Content-Length 与 Content-Range 都可直接确定。
        headers = {
            "Content-Length": str(total_bytes),
            "Content-Range": (
                f"bytes {only.start}-{only.end}/{req.object_length}"
            ),
        }
    else:
        # 多段：实际 Content-Length 取决于后续 /range-response 生成的
        # multipart 分帧（boundary/part 头），决策不臆造该值；代理在
        # 组装完成后填入或改用分块传输。content_length 仅统计各可满足
        # 段的范围字节之和（不含分帧开销）。
        media_type = "multipart/byteranges"
        headers = {}

    return ClientRangeDecision(
        status_code=206,
        reason="partial",
        headers=headers,
        object_length=req.object_length,
        media_type=media_type,
        content_length=total_bytes,
        segments=segments,
        satisfied_count=satisfied,
        skipped_count=skipped,
        if_range_matched=True,
        if_range_kind=if_range_kind,
        plan=plan_result,
    )


@app.post("/client-range-decision", response_model=ClientRangeDecision)
def client_range_decision(
    req: ClientRangeDecisionRequest,
) -> ClientRangeDecision:
    """HTTP 包装：决策逻辑本身见 run_client_range_decision。"""
    return run_client_range_decision(req)


# ------------------------- /conditional-range-decision -------------------------

# If-None-Match 的 entity-tag（RFC 9110 §8.8.3）：可选 W/ + 双引号包裹的
# opaque-tag；星号形式单独解析。
_ENTITY_TAG_RE = re.compile(r'(?:(W/))?("[\x21\x23-\x7e\x80-\xff]*")', re.IGNORECASE)


class ConditionalRangeDecisionRequest(ClientRangeDecisionRequest):
    """/conditional-range-decision 请求：在 /client-range-decision 全部字段
    之上增加客户端的 If-None-Match 与 If-Modified-Since 条件头。

    两个字段均省略或为 null 表示未携带该条件；显式空字符串/空白属于非法
    头值（422）。If-None-Match 存在时，If-Modified-Since 按 RFC 9110 被
    忽略（连语法都不评估）。
    """

    if_none_match: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="客户端 If-None-Match 头原值：'*' 或逗号分隔的 entity-tag 列表",
    )
    if_modified_since: str | None = Field(
        None,
        max_length=_CLIENT_HEADER_MAX_LENGTH,
        description="客户端 If-Modified-Since 头原值（IMF-fixdate）；"
        "If-None-Match 存在时按 RFC 9110 忽略",
    )


class ConditionEvaluation(BaseModel):
    """GET 前置条件（If-None-Match / If-Modified-Since）的评估结果。"""

    condition_present: bool = Field(
        ..., description="是否携带了至少一个前置条件头"
    )
    matched: bool = Field(
        ...,
        description="前置条件是否命中（命中时服务应回 304 Not Modified）",
    )
    matched_by: Literal["if_none_match", "if_modified_since"] | None = Field(
        None,
        description="命中条件的来源：if_none_match（'*' 或标签弱比较匹配）或 "
        "if_modified_since（当前修改时间不晚于该日期）；未命中/无条件为 null",
    )
    if_none_match_present: bool = Field(
        ..., description="是否携带 If-None-Match（存在时 If-Modified-Since 被忽略）"
    )
    if_none_match_kind: Literal["*", "list"] | None = Field(
        None, description="If-None-Match 形式；未携带时为 null"
    )
    if_none_match_tags: list[str] | None = Field(
        None,
        description="list 形式解析出的各 entity-tag 原值（去成员间 OWS，含可能的 W/ 前缀）",
    )
    if_modified_since_evaluated: bool = Field(
        ...,
        description="是否实际评估了 If-Modified-Since（未携带或因 If-None-Match "
        "存在而被忽略时为 false）",
    )


class ConditionalRangeDecision(BaseModel):
    """条件命中时为 304（无 plan）；未命中时 decision 内嵌完整的
    /client-range-decision 结果（200/206/416）。"""

    status_code: Literal[304, 200, 206, 416] = Field(
        ..., description="应对客户端使用的 HTTP 状态码（顶层便于快速分支）"
    )
    reason: str = Field(
        ...,
        description="304 时为 not_modified；其余沿用 decision.reason "
        "（range_absent / if_range_mismatch / partial / unsatisfiable）",
    )
    headers: dict[str, str] = Field(
        ...,
        description="应随响应下发的头字段。304 时携带当前 ETag/Last-Modified "
        "验证器；其余与 /client-range-decision 完全一致",
    )
    object_length: int
    condition: ConditionEvaluation = Field(..., description="前置条件评估明细")
    decision: ClientRangeDecision | None = Field(
        None,
        description="条件未命中时的完整 /client-range-decision 决策；304 时为 null",
    )


def _parse_if_none_match(raw: str) -> tuple[Literal["*", "list"], list[str]]:
    """严格按 RFC 9110 §8.8.3 解析 If-None-Match 头值。

    接受 ``*`` 或逗号分隔的 entity-tag 列表（每个标签为可选 ``W/`` 前缀
    加双引号 opaque-tag）；成员间允许 OWS，空成员（重复逗号）、尾随逗号、
    裸文本、残缺引号一律拒绝。返回 (kind, tags)：``*`` 时 tags 为空，
    list 时 tags 为去成员间 OWS 的标签原值（保留 W/ 前缀，大小写归一为 W/）。
    """
    loc = "if_none_match"
    _reject_header_chars(raw, loc)
    text = raw.strip(_OWS_STRIP)
    if not text:
        raise ClientRangeError(
            "If-None-Match 头为空：省略/null 表示未携带，空字符串是非法头值",
            loc,
            code="invalid_if_none_match",
        )
    if text == "*":
        return "*", []

    tags: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _ENTITY_TAG_RE.match(text, pos)
        if m is None:
            raise ClientRangeError(
                "If-None-Match 必须是 '*' 或逗号分隔的 entity-tag 列表"
                '（每个标签形如 "v1" 或 W/"v1"）',
                loc,
                code="invalid_if_none_match",
            )
        weak_prefix, opaque = m.group(1), m.group(2)
        tags.append(("W/" if weak_prefix else "") + opaque)
        pos = m.end()
        while pos < n and text[pos] in _OWS_STRIP:
            pos += 1
        if pos == n:
            break
        if text[pos] != ",":
            raise ClientRangeError(
                "If-None-Match 的 entity-tag 之间必须以逗号分隔，"
                "不允许尾随其他文本",
                loc,
                code="invalid_if_none_match",
            )
        pos += 1
        while pos < n and text[pos] in _OWS_STRIP:
            pos += 1
        if pos == n:
            # 尾随逗号：列表以空成员结束，按非法头值拒绝。
            raise ClientRangeError(
                "If-None-Match 的标签列表不允许尾随逗号或空成员",
                loc,
                code="invalid_if_none_match",
            )
    return "list", tags


def _strip_weak_prefix(tag: str) -> str:
    """去掉 entity-tag 的 W/ 前缀（大小写不敏感），返回 opaque-tag。"""
    return tag[2:] if tag[:2].lower() == "w/" else tag


@app.post(
    "/conditional-range-decision",
    response_model=ConditionalRangeDecision,
)
def conditional_range_decision(
    req: ConditionalRangeDecisionRequest,
) -> ConditionalRangeDecision:
    """在 /client-range-decision 决策之前先评估 GET 前置条件。

    1. 校验当前验证器（强 ETag / Last-Modified）；
    2. 条件命中（If-None-Match 的 ``*``/标签弱比较匹配，或仅携带
       If-Modified-Since 且当前修改时间不晚于该日期）时直接给出 304
       决策，回传当前验证器且不解析 Range、不生成回源计划；
    3. 未命中时完整复用 run_client_range_decision 的 200/206/416 决策，
       206 的 plan 仍可直接提交 /assemble。
    """
    # 0. 当前验证器：语义与 /client-range-decision 完全一致。
    current_etag: str | None = None
    if req.etag is not None:
        current_etag = _parse_current_strong_etag(req.etag)
    current_lm: datetime.datetime | None = None
    if req.last_modified is not None:
        current_lm = _parse_http_date(
            req.last_modified, "last_modified", code="invalid_last_modified"
        )

    # 1. If-None-Match 语法解析（显式空白为非法头值）。
    inm_kind: Literal["*", "list"] | None = None
    inm_tags: list[str] | None = None
    if req.if_none_match is not None:
        inm_kind, parsed_tags = _parse_if_none_match(req.if_none_match)
        # "*" 形式没有标签成员，对外显式给出 null。
        inm_tags = parsed_tags if inm_kind == "list" else None

    # 2. 评估前置条件。If-None-Match 存在时按 RFC 9110 完全忽略
    #    If-Modified-Since（连语法都不评估）。
    matched = False
    matched_by: Literal["if_none_match", "if_modified_since"] | None = None
    ims_evaluated = False
    if inm_kind is not None:
        if inm_kind == "*":
            # *：表示源站存在该表示即匹配；代理侧对象始终存在，直接命中。
            matched = True
        elif current_etag is not None:
            # 弱比较：剥掉双方 W/ 前缀后逐字节全等即命中（任一标签匹配即可）。
            current_opaque = _strip_weak_prefix(current_etag)
            matched = any(
                _strip_weak_prefix(tag) == current_opaque for tag in inm_tags
            )
        if matched:
            matched_by = "if_none_match"
    elif req.if_modified_since is not None:
        ims_dt = _parse_http_date(
            req.if_modified_since,
            "if_modified_since",
            code="invalid_if_modified_since",
        )
        ims_evaluated = True
        # 当前修改时间早于或等于条件时刻即“未修改”；没有当前
        # Last-Modified 可比较时视为未命中（继续正常决策）。
        if current_lm is not None and current_lm <= ims_dt:
            matched = True
            matched_by = "if_modified_since"

    condition = ConditionEvaluation(
        condition_present=inm_kind is not None or req.if_modified_since is not None,
        matched=matched,
        matched_by=matched_by,
        if_none_match_present=inm_kind is not None,
        if_none_match_kind=inm_kind,
        if_none_match_tags=inm_tags,
        if_modified_since_evaluated=ims_evaluated,
    )

    # 3. 条件命中：304 + 当前验证器；不解析 Range/If-Range，不生成 plan。
    if matched:
        headers: dict[str, str] = {}
        if current_etag is not None:
            headers["ETag"] = current_etag
        if current_lm is not None:
            headers["Last-Modified"] = _format_imf_fixdate(current_lm)
        return ConditionalRangeDecision(
            status_code=304,
            reason="not_modified",
            headers=headers,
            object_length=req.object_length,
            condition=condition,
            decision=None,
        )

    # 4. 未命中：复用 /client-range-decision 的完整 200/206/416 决策。
    decision = run_client_range_decision(req)
    return ConditionalRangeDecision(
        status_code=decision.status_code,
        reason=decision.reason,
        headers=decision.headers,
        object_length=req.object_length,
        condition=condition,
        decision=decision,
    )


# ------------------------- /cache-write-decision -------------------------

# 头值与 URI 的长度上限（与客户端决策端点一致，抵御超长输入）。
_CACHE_WRITE_VALUE_MAX_LENGTH = 8192
# 原请求头字段名长度上限与条目数上限。
_CACHE_WRITE_HEADER_NAME_MAX_LENGTH = 256
_CACHE_WRITE_HEADER_MAX_ITEMS = 128


class CacheWriteDecisionRequest(BaseModel):
    """/cache-write-decision 请求：单个已验收 206 分块的共享缓存写入判定要素。

    etag 为 /verify-fetch-batch 确认（或与之一致）的强 ETag 原值；
    request_headers 为代理收到的原请求头（字段名大小写不敏感，大小写不同
    的同名头按接收顺序以 ", " 合并）；cache_control / age / vary 为上游
    206 响应携带的同名头原值，省略或 null 表示上游未携带。
    """

    uri: str = Field(
        ...,
        min_length=1,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="对象 URI（原样进入缓存键，不做规范化）",
    )
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="块起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="块结束字节偏移（含）")
    etag: str = Field(
        ...,
        min_length=1,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="已确认的强 ETag 头原值（含引号）；弱标签一律拒绝",
    )
    request_headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=_CACHE_WRITE_HEADER_MAX_ITEMS,
        description="原请求头（字段名大小写不敏感）；用于 Authorization 检查与 Vary 取值",
    )
    cache_control: str | None = Field(
        None,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="上游 Cache-Control 头原值；省略/null 表示未携带",
    )
    age: str | None = Field(
        None,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="上游 Age 头原值（delta-seconds）；省略/null 按 0 处理",
    )
    vary: str | None = Field(
        None,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="上游 Vary 头原值；省略/null 表示未携带",
    )

    @model_validator(mode="after")
    def _check_block_range(self) -> "CacheWriteDecisionRequest":
        if self.end < self.start:
            raise ValueError("end 不得小于 start")
        return self


CacheWriteReason = Literal[
    "cacheable",
    "no_store",
    "private",
    "vary_star",
    "authorization_not_shareable",
    "no_explicit_freshness",
    "expired",
]


class CacheWriteDecisionResponse(BaseModel):
    """共享缓存写入判定：判定结果、原因、剩余秒数与稳定缓存键。"""

    cacheable: bool = Field(..., description="该分块是否允许写入共享缓存")
    reason: CacheWriteReason = Field(
        ...,
        description="判定原因：cacheable=可写入；no_store=响应禁止存储；"
        "private=响应仅限私有缓存；vary_star=Vary: * 无法构造缓存键；"
        "authorization_not_shareable=请求带 Authorization 且响应未显式允许共享；"
        "no_explicit_freshness=缺少 s-maxage/max-age；expired=扣除 Age 后已过期",
    )
    remaining_seconds: int | None = Field(
        ...,
        description="可缓存时扣除 Age 后的剩余新鲜秒数（>=1）；不可缓存为 null",
    )
    cache_key: str | None = Field(
        ...,
        description="可缓存时的稳定缓存键（规范化成分的 SHA-256 十六进制）；不可缓存为 null",
    )
    vary_headers: list[str] = Field(
        ...,
        description="Vary 解析出的请求头字段名（小写、去重、排序）；无 Vary 或 Vary: * 时为空列表",
    )


class CacheWriteError(Exception):
    """共享缓存写入判定阶段的头值/字段校验失败，统一对应 HTTP 422。

    loc 为可定位的字段路径："uri"、"etag"、"cache_control"、"age"、
    "vary"、"request_headers.<name>"。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        code: str = "cache_write_error",
    ):
        self.message = message
        self.loc = loc
        self.code = code
        super().__init__(message)


@app.exception_handler(CacheWriteError)
async def cache_write_error_handler(request: Request, exc: CacheWriteError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


def _check_cache_write_chars(raw: str, loc: str) -> None:
    """拒绝头值中的 CR/LF 与其他控制字符（HTAB 作为合法 OWS 放行）。"""
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in raw):
        raise CacheWriteError(
            "头值不允许包含 CR/LF 或其他控制字符（拒绝折行与头部注入）",
            loc,
            code="malformed_header",
        )


def _validate_cache_write_uri(raw: str) -> None:
    """URI 不得为空白串且不得含控制字符（原样进入缓存键，不做规范化）。"""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise CacheWriteError(
            "uri 不允许包含控制字符", "uri", code="invalid_uri"
        )
    if not raw.strip(_OWS_STRIP):
        raise CacheWriteError(
            "uri 不能为空白字符串", "uri", code="invalid_uri"
        )


def _parse_confirmed_strong_etag(raw: str) -> str:
    """校验已确认的强 ETag，返回去首尾 OWS 后的 opaque-tag 原值。"""
    loc = "etag"
    _check_cache_write_chars(raw, loc)
    value = raw.strip(_OWS_STRIP)
    if not value:
        raise CacheWriteError(
            "etag 为空白：必须提供已确认的强 ETag"
            "（如 /verify-fetch-batch 的确认结果）",
            loc,
            code="missing_etag",
        )
    if value[:2].lower() == "w/":
        raise CacheWriteError(
            "仅接受强 ETag：弱 ETag（W/ 前缀）不能作为共享缓存的版本标识",
            loc,
            code="weak_etag",
        )
    if not _STRONG_ETAG_RE.fullmatch(value):
        raise CacheWriteError(
            "etag 不是合法的强 ETag：必须为双引号包裹的 opaque-tag（如 "
            '"v1"），不得包含空格、控制字符或裸引号',
            loc,
            code="invalid_etag",
        )
    return value


def _normalize_cache_write_headers(raw: dict[str, str]) -> dict[str, str]:
    """把原请求头规范化为 {小写字段名: 去 OWS 值}，重复名按出现顺序以 ", " 合并。

    字段名必须是合法 ASCII token，取值不得含 CR/LF 等控制字符；大小写不同
    的同名头按 RFC 9110 的列表语义合并，保证缓存键与字段名大小写无关。
    """
    normalized: dict[str, str] = {}
    for name, value in raw.items():
        loc = f"request_headers.{name}"
        if len(name) > _CACHE_WRITE_HEADER_NAME_MAX_LENGTH:
            raise CacheWriteError(
                "请求头字段名超长",
                loc,
                code="invalid_request_header",
            )
        try:
            name_b = name.encode("ascii")
        except UnicodeEncodeError:
            raise CacheWriteError(
                f"请求头字段名 {name!r} 必须是 ASCII token",
                loc,
                code="invalid_request_header",
            )
        if not _TOKEN_RE.fullmatch(name_b):
            raise CacheWriteError(
                f"请求头字段名 {name!r} 不是合法 token",
                loc,
                code="invalid_request_header",
            )
        if len(value) > _CACHE_WRITE_VALUE_MAX_LENGTH:
            raise CacheWriteError(
                f"请求头 {name!r} 的取值超长",
                loc,
                code="invalid_request_header",
            )
        _check_cache_write_chars(value, loc)
        lname = name.lower()
        stripped = value.strip(_OWS_STRIP)
        if lname in normalized:
            normalized[lname] = f"{normalized[lname]}, {stripped}"
        else:
            normalized[lname] = stripped
    return normalized


def _parse_cache_control_directives(raw: str) -> list[tuple[str, str | None]]:
    """按 HTTP 指令语法解析 Cache-Control，返回 [(小写指令名, 取值或 None)]。

    文法：``#( token [ "=" ( token / quoted-string ) ] )``——成员以逗号
    分隔（引号内的逗号不分隔），成员前后及 "=" 周围允许 OWS/BWS，空成员
    按 RFC 9110 §5.6.1.1 忽略；quoted-string 支持 quoted-pair 转义并在
    返回前还原。指令名大小写不敏感（统一小写返回）。语法错误抛
    CacheWriteError(loc="cache_control")。
    """
    loc = "cache_control"
    _check_cache_write_chars(raw, loc)
    try:
        data = raw.encode("ascii")
    except UnicodeEncodeError:
        raise CacheWriteError(
            "Cache-Control 必须是 ASCII 文本", loc, code="invalid_cache_control"
        )
    if not data.strip(b" \t"):
        raise CacheWriteError(
            "Cache-Control 头为空白：省略/null 表示未携带，空字符串是非法头值",
            loc,
            code="invalid_cache_control",
        )

    directives: list[tuple[str, str | None]] = []
    n = len(data)
    pos = 0
    while pos < n:
        # 跳过 OWS 与空成员（连续/首尾逗号按列表规则忽略）。
        while pos < n and data[pos] in (0x20, 0x09, 0x2C):
            pos += 1
        if pos >= n:
            break
        start = pos
        while pos < n and data[pos] not in (0x3D, 0x2C, 0x20, 0x09):  # '=' ',' SP TAB
            pos += 1
        name_b = data[start:pos]
        if not _TOKEN_RE.fullmatch(name_b):
            raise CacheWriteError(
                f"Cache-Control 指令名 {name_b.decode('ascii', 'replace')!r} 非法",
                loc,
                code="invalid_cache_control",
            )
        name = name_b.decode("ascii")
        while pos < n and data[pos] in (0x20, 0x09):
            pos += 1
        value: str | None = None
        if pos < n and data[pos] == 0x3D:  # '='
            pos += 1
            while pos < n and data[pos] in (0x20, 0x09):
                pos += 1
            if pos >= n or data[pos] == 0x2C:
                raise CacheWriteError(
                    f"Cache-Control 指令 {name!r} 缺少取值",
                    loc,
                    code="invalid_cache_control",
                )
            if data[pos] == 0x22:  # '"' quoted-string
                pos += 1
                out = bytearray()
                closed = False
                while pos < n:
                    ch = data[pos]
                    if ch == 0x22:
                        closed = True
                        pos += 1
                        break
                    if ch == 0x5C:  # '\\' quoted-pair
                        pos += 1
                        if pos >= n:
                            raise CacheWriteError(
                                "Cache-Control 的 quoted-pair 转义被截断",
                                loc,
                                code="invalid_cache_control",
                            )
                        esc = data[pos]
                        if not (esc == 0x09 or 0x20 <= esc <= 0x7E):
                            raise CacheWriteError(
                                "Cache-Control 的 quoted-pair 只能转义 HTAB/可见 ASCII",
                                loc,
                                code="invalid_cache_control",
                            )
                        out.append(esc)
                        pos += 1
                    else:
                        if (ch < 0x20 and ch != 0x09) or ch == 0x7F:
                            raise CacheWriteError(
                                "Cache-Control 的 quoted-string 含控制字符",
                                loc,
                                code="invalid_cache_control",
                            )
                        out.append(ch)
                        pos += 1
                if not closed:
                    raise CacheWriteError(
                        "Cache-Control 的 quoted-string 未闭合（缺少结尾 '\"'）",
                        loc,
                        code="invalid_cache_control",
                    )
                value = bytes(out).decode("ascii")
            else:
                vstart = pos
                while pos < n and data[pos] not in (0x2C, 0x20, 0x09):
                    pos += 1
                vtoken = data[vstart:pos]
                if not _TOKEN_RE.fullmatch(vtoken):
                    raise CacheWriteError(
                        f"Cache-Control 指令 {name!r} 的取值既不是合法 token "
                        "也不是 quoted-string",
                        loc,
                        code="invalid_cache_control",
                    )
                value = vtoken.decode("ascii")
            while pos < n and data[pos] in (0x20, 0x09):
                pos += 1
            if pos < n and data[pos] != 0x2C:
                raise CacheWriteError(
                    f"Cache-Control 指令 {name!r} 的取值后存在非法字符",
                    loc,
                    code="invalid_cache_control",
                )
        elif pos < n and data[pos] != 0x2C:
            raise CacheWriteError(
                f"Cache-Control 指令 {name!r} 后存在非法字符",
                loc,
                code="invalid_cache_control",
            )
        directives.append((name.lower(), value))

    if not directives:
        raise CacheWriteError(
            "Cache-Control 头不包含任何指令",
            loc,
            code="invalid_cache_control",
        )
    return directives


def _freshness_delta_seconds(
    directives: list[tuple[str, str | None]], name: str
) -> int | None:
    """提取 max-age / s-maxage 的 delta-seconds；重复或取值非法时报 422。"""
    loc = "cache_control"
    values = [v for n, v in directives if n == name]
    if not values:
        return None
    if len(values) > 1:
        raise CacheWriteError(
            f"Cache-Control 中 {name} 指令重复出现：共享缓存不得猜测相互"
            "冲突的新鲜度语义",
            loc,
            code="duplicate_cache_directive",
        )
    value = values[0]
    if value is None or not value.isdigit():
        raise CacheWriteError(
            f"Cache-Control 指令 {name} 的取值必须是 delta-seconds"
            "（非负十进制整数，token 或引号包裹形式均可）",
            loc,
            code="invalid_cache_control",
        )
    return int(value)


def _parse_cache_write_age(raw: str) -> int:
    """解析 Age 头为 delta-seconds；非法取值报 422。"""
    loc = "age"
    _check_cache_write_chars(raw, loc)
    value = raw.strip(_OWS_STRIP)
    if not value:
        raise CacheWriteError(
            "Age 头为空白：省略/null 表示未携带（按 0 处理），空字符串是非法头值",
            loc,
            code="invalid_age",
        )
    if not value.isascii() or not value.isdigit():
        raise CacheWriteError(
            "Age 必须是 delta-seconds（非负十进制整数，不接受引号/符号/小数）",
            loc,
            code="invalid_age",
        )
    return int(value)


def _parse_cache_write_vary(raw: str) -> tuple[bool, list[str]]:
    """解析 Vary 头，返回 (是否含 *, 小写字段名列表（去重排序）)。

    成员间允许 OWS，空成员按 RFC 9110 列表规则忽略；成员必须是 "*" 或
    合法字段名 token。显式空白或不含任何有效成员的 Vary 视为非法头值。
    """
    loc = "vary"
    _check_cache_write_chars(raw, loc)
    text = raw.strip(_OWS_STRIP)
    if not text:
        raise CacheWriteError(
            "Vary 头为空白：省略/null 表示未携带，空字符串是非法头值",
            loc,
            code="invalid_vary",
        )
    has_star = False
    fields: set[str] = set()
    for member in text.split(","):
        token = member.strip(_OWS_STRIP)
        if not token:
            continue  # 空成员：RFC 9110 §5.6.1.1 要求忽略
        if token == "*":
            has_star = True
            continue
        try:
            token_b = token.encode("ascii")
        except UnicodeEncodeError:
            raise CacheWriteError(
                f"Vary 成员 {token!r} 必须是 ASCII 字段名",
                loc,
                code="invalid_vary",
            )
        if not _TOKEN_RE.fullmatch(token_b):
            raise CacheWriteError(
                f"Vary 成员 {token!r} 不是合法的字段名 token",
                loc,
                code="invalid_vary",
            )
        fields.add(token.lower())
    if not has_star and not fields:
        raise CacheWriteError(
            "Vary 头不包含任何有效成员（'*' 或字段名）",
            loc,
            code="invalid_vary",
        )
    return has_star, sorted(fields)


def _build_cache_write_key(
    uri: str,
    start: int,
    end: int,
    etag: str,
    vary_fields: list[str],
    headers: dict[str, str],
) -> str:
    """构造稳定缓存键：URI、闭区间、ETag 与 Vary 指定的原请求头值经固定
    JSON 规范化后取 SHA-256 十六进制。

    规范化保证：字段名小写且按字典序排列、缺失头以 null 表示、取值仅去
    首尾 OWS（保留原大小写与内部空白）——同一组语义的输入必得同一键。
    """
    material = {
        "uri": uri,
        "start": start,
        "end": end,
        "etag": etag,
        "vary": [[name, headers.get(name)] for name in vary_fields],
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


@app.post("/cache-write-decision", response_model=CacheWriteDecisionResponse)
def cache_write_decision(
    req: CacheWriteDecisionRequest,
) -> CacheWriteDecisionResponse:
    """判断单个已验收 206 分块能否写入共享缓存，并给出稳定缓存键。

    判定顺序：先校验全部头值语法（非法即 422），再依次检查 no-store、
    private、Vary: *、Authorization 共享许可，最后按 s-maxage 优先于
    max-age 计算剩余新鲜秒数（扣除 Age，过期即拒绝）。可缓存时返回
    剩余秒数与包含 URI、区间、ETag、Vary 请求头值的稳定缓存键。
    """
    # 1. 头值与字段语法校验（任一非法即 422，先于任何判定）。
    _validate_cache_write_uri(req.uri)
    etag = _parse_confirmed_strong_etag(req.etag)
    headers = _normalize_cache_write_headers(req.request_headers)
    directives = (
        _parse_cache_control_directives(req.cache_control)
        if req.cache_control is not None
        else []
    )
    s_maxage = _freshness_delta_seconds(directives, "s-maxage")
    max_age = _freshness_delta_seconds(directives, "max-age")
    age = _parse_cache_write_age(req.age) if req.age is not None else 0
    vary_star, vary_fields = (
        _parse_cache_write_vary(req.vary) if req.vary is not None else (False, [])
    )

    directive_names = {name for name, _ in directives}

    def reject(reason: CacheWriteReason) -> CacheWriteDecisionResponse:
        return CacheWriteDecisionResponse(
            cacheable=False,
            reason=reason,
            remaining_seconds=None,
            cache_key=None,
            vary_headers=vary_fields,
        )

    # 2. 禁止写入共享缓存的响应指令（RFC 9111 §5.2.2.5 / §5.2.2.7）。
    if "no-store" in directive_names:
        return reject("no_store")
    if "private" in directive_names:
        return reject("private")
    # 3. Vary: * 无法构造可复用的缓存键（RFC 9110 §12.5.5）。
    if vary_star:
        return reject("vary_star")
    # 4. 带 Authorization 的请求：仅当响应显式允许共享缓存（must-revalidate /
    #    public / s-maxage，RFC 9111 §3.5）时才可写入。
    if "authorization" in headers and not (
        {"must-revalidate", "public", "s-maxage"} & directive_names
    ):
        return reject("authorization_not_shareable")

    # 5. 新鲜度：s-maxage 优先于 max-age（RFC 9111 §5.2.2.10）；两者皆无
    #    时共享缓存不得凭启发式写入。
    lifetime = s_maxage if s_maxage is not None else max_age
    if lifetime is None:
        return reject("no_explicit_freshness")
    remaining = lifetime - age
    if remaining <= 0:
        return reject("expired")

    # 6. 可写入：构造稳定缓存键（URI + 区间 + ETag + Vary 指定的请求头值）。
    cache_key = _build_cache_write_key(
        req.uri, req.start, req.end, etag, vary_fields, headers
    )
    return CacheWriteDecisionResponse(
        cacheable=True,
        reason="cacheable",
        remaining_seconds=remaining,
        cache_key=cache_key,
        vary_headers=vary_fields,
    )


# ------------------------- /cache-read-plan -------------------------

# 候选块 cache_key 的格式：/cache-write-decision 输出的 SHA-256 小写十六进制。
_CACHE_KEY_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class CacheReadCandidate(BaseModel):
    """候选缓存块：写入共享缓存时由 /cache-write-decision 确定的元数据。"""

    cache_id: CacheId = Field(..., description="缓存块的稳定 id（请求内唯一）")
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="缓存结束字节偏移（含）")
    cache_key: str = Field(
        ...,
        max_length=64,
        description="写入时 /cache-write-decision 返回的稳定缓存键（64 位小写十六进制）",
    )
    vary_headers: list[str] = Field(
        default_factory=list,
        max_length=_CACHE_WRITE_HEADER_MAX_ITEMS,
        description="写入时响应的 vary_headers（Vary 指定的请求头字段名）；无 Vary 时为 []",
    )
    stored_at: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="写入时刻（Unix 秒）"
    )
    remaining_seconds: Annotated[int, StrictInt] = Field(
        ..., ge=1, description="写入时判定的剩余新鲜秒数（>=1）"
    )


class CacheReadPlanRequest(BaseModel):
    """/cache-read-plan 请求：用当前请求要素重算键，筛选可用缓存块并规划回源。

    uri / etag / request_headers 为当前请求的要素，必须与写入时语义一致
    （etag 为对象当前强 ETag）；now 为查询时刻（Unix 秒）；ranges 与
    max_gap / fetch_limit 的语义与 /plan 完全一致。
    """

    uri: str = Field(
        ...,
        min_length=1,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="对象 URI（与写入时一致，原样参与重算键，不做规范化）",
    )
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    etag: str = Field(
        ...,
        min_length=1,
        max_length=_CACHE_WRITE_VALUE_MAX_LENGTH,
        description="对象当前的强 ETag 头原值（含引号）；弱标签一律拒绝",
    )
    request_headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=_CACHE_WRITE_HEADER_MAX_ITEMS,
        description="当前请求头（字段名大小写不敏感）；用于重算缓存键的 Vary 成分",
    )
    now: Annotated[int, StrictInt] = Field(
        ..., ge=0, description="查询时刻（Unix 秒）；stored_at + remaining_seconds 必须严格大于它"
    )
    ranges: list[RangeSpec] = Field(..., min_length=1, description="目标范围列表，可为闭区间或 suffix")
    max_gap: Annotated[int, StrictInt] = Field(..., ge=0, description="允许合并的最大间隙（字节），透传给 /plan")
    fetch_limit: Annotated[int, StrictInt] = Field(..., ge=1, description="单次回源上限（字节），透传给 /plan")
    candidates: list[CacheReadCandidate] = Field(
        default_factory=list, description="候选缓存块列表（可为空）"
    )


CacheReadCandidateReason = Literal[
    "adopted", "not_referenced", "key_mismatch", "expired"
]


class CacheReadCandidateStatus(BaseModel):
    """单个候选缓存块的采用状态；未采用时 reason 给出原因。"""

    index: int = Field(..., description="候选块在请求 candidates 中的下标（从 0 起）")
    cache_id: CacheId = Field(..., description="候选块的稳定 id（回显）")
    adopted: bool = Field(
        ..., description="是否被采用（进入 plan 且被目标范围实际引用）"
    )
    reason: CacheReadCandidateReason = Field(
        ...,
        description="adopted=已采用；not_referenced=可用但未与任何目标范围相交；"
        "key_mismatch=按当前请求重算的键与 cache_key 不一致（URI/版本/Vary 变体不符）；"
        "expired=stored_at + remaining_seconds 不晚于查询时刻",
    )
    expires_at: int = Field(
        ..., description="到期时刻（stored_at + remaining_seconds，Unix 秒）"
    )


class CacheReadPlanResponse(BaseModel):
    """缓存读取规划结果：plan 可直接提交 /assemble。"""

    plan: PlanResponse = Field(
        ..., description="基于可用缓存块的 /plan 结果，可直接用于回源与 /assemble"
    )
    candidates: list[CacheReadCandidateStatus] = Field(
        ..., description="按输入顺序逐块给出的采用状态与未采用原因"
    )


class CacheReadPlanError(Exception):
    """缓存读取规划阶段的字段/语义校验失败，统一对应 HTTP 422。

    loc 为可定位的字段路径，例如 "candidates[1].cache_id"、"etag"、
    "request_headers.<name>"；cache_id 在可归属到某个候选块时给出。
    """

    def __init__(
        self,
        message: str,
        loc: str | None = None,
        *,
        cache_id: str | int | None = None,
        code: str = "cache_read_plan_error",
    ):
        self.message = message
        self.loc = loc
        self.cache_id = cache_id
        self.code = code
        super().__init__(message)


@app.exception_handler(CacheReadPlanError)
async def cache_read_plan_error_handler(request: Request, exc: CacheReadPlanError):
    detail: dict[str, object] = {"message": exc.message}
    if exc.loc is not None:
        detail["loc"] = exc.loc
    if exc.cache_id is not None:
        detail["cache_id"] = exc.cache_id
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "detail": [detail]},
    )


def _normalize_candidate_vary_headers(raw: list[str], loc: str) -> list[str]:
    """把候选块的 vary_headers 规范化为小写、去重、排序的字段名列表。

    与 /cache-write-decision 解析 Vary 头时的规范化一致，保证重算键使用
    的 vary 成分与写入侧逐字节相同；成员必须是合法字段名 token（'*' 的
    响应本就无法写入共享缓存，同样拒绝）。
    """
    fields: set[str] = set()
    for pos, name in enumerate(raw):
        member_loc = f"{loc}[{pos}]"
        if len(name) > _CACHE_WRITE_HEADER_NAME_MAX_LENGTH:
            raise CacheReadPlanError(
                "vary_headers 成员超长", member_loc, code="invalid_vary_headers"
            )
        if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in name):
            raise CacheReadPlanError(
                "vary_headers 成员不允许包含 CR/LF 或其他控制字符",
                member_loc,
                code="invalid_vary_headers",
            )
        token = name.strip(_OWS_STRIP)
        if token == "*":
            raise CacheReadPlanError(
                "vary_headers 不允许包含 '*'：Vary: * 的响应本就无法写入共享缓存",
                member_loc,
                code="invalid_vary_headers",
            )
        try:
            token_b = token.encode("ascii")
        except UnicodeEncodeError:
            raise CacheReadPlanError(
                f"vary_headers 成员 {token!r} 必须是 ASCII 字段名",
                member_loc,
                code="invalid_vary_headers",
            )
        if not _TOKEN_RE.fullmatch(token_b):
            raise CacheReadPlanError(
                f"vary_headers 成员 {token!r} 不是合法的字段名 token",
                member_loc,
                code="invalid_vary_headers",
            )
        fields.add(token.lower())
    return sorted(fields)


@app.post("/cache-read-plan", response_model=CacheReadPlanResponse)
def cache_read_plan(req: CacheReadPlanRequest) -> CacheReadPlanResponse:
    """筛选可用候选缓存块，并生成可直接用于 /assemble 的回源规划。

    1. 用当前 uri / etag / request_headers 按 /cache-write-decision 的
       规范化与键算法，结合候选块的区间与 vary_headers 逐块重算缓存键；
    2. 仅保留重算键与 cache_key 全等（键材料含 ETag 与 Vary 取值，故键
       匹配即版本与 Vary 变体一致）且 stored_at + remaining_seconds 严格
       大于 now 的候选块；
    3. 可用块按到期时间降序、输入次序升序作为 cached_blocks 交给缓存感知
       规划（相互重叠的字节优先由更耐久的块提供）；
    4. 返回 plan 与逐块采用状态/未采用原因。
    """
    # 1. 请求级要素：与写入侧完全相同的校验与规范化（错误码/loc 一致），
    #    保证重算键与写入键逐字节可比。
    try:
        _validate_cache_write_uri(req.uri)
        etag = _parse_confirmed_strong_etag(req.etag)
        headers = _normalize_cache_write_headers(req.request_headers)
    except CacheWriteError as exc:
        raise CacheReadPlanError(exc.message, exc.loc, code=exc.code) from exc

    # 2. 逐块校验（区间、cache_key 格式、vary_headers、id 唯一）并筛选可用块。
    seen_ids: set[object] = set()
    statuses: dict[int, CacheReadCandidateStatus] = {}
    usable: list[tuple[int, int, CacheReadCandidate]] = []  # (expires_at, pos, cand)
    for pos, cand in enumerate(req.candidates):
        loc = f"candidates[{pos}]"
        if cand.end < cand.start:
            raise CacheReadPlanError(
                f"候选块 {cand.cache_id!r} 的区间 [{cand.start},{cand.end}] 非法："
                "end 不得小于 start",
                loc,
                cache_id=cand.cache_id,
                code="invalid_interval",
            )
        if cand.end >= req.object_length:
            raise CacheReadPlanError(
                f"候选块 {cand.cache_id!r} [{cand.start},{cand.end}] 越界："
                f"对象长度为 {req.object_length}",
                loc,
                cache_id=cand.cache_id,
                code="interval_out_of_bounds",
            )
        if not _CACHE_KEY_HEX_RE.fullmatch(cand.cache_key):
            raise CacheReadPlanError(
                "cache_key 必须是 /cache-write-decision 输出的 64 位小写十六进制"
                " SHA-256",
                f"{loc}.cache_key",
                cache_id=cand.cache_id,
                code="invalid_cache_key",
            )
        vary_fields = _normalize_candidate_vary_headers(
            cand.vary_headers, f"{loc}.vary_headers"
        )
        if cand.cache_id in seen_ids:
            raise CacheReadPlanError(
                f"候选块 cache_id {cand.cache_id!r} 重复",
                f"{loc}.cache_id",
                cache_id=cand.cache_id,
                code="duplicate_cache_id",
            )
        seen_ids.add(cand.cache_id)

        expires_at = cand.stored_at + cand.remaining_seconds
        recomputed = _build_cache_write_key(
            req.uri, cand.start, cand.end, etag, vary_fields, headers
        )
        if recomputed != cand.cache_key:
            statuses[pos] = CacheReadCandidateStatus(
                index=pos,
                cache_id=cand.cache_id,
                adopted=False,
                reason="key_mismatch",
                expires_at=expires_at,
            )
            continue
        if expires_at <= req.now:
            statuses[pos] = CacheReadCandidateStatus(
                index=pos,
                cache_id=cand.cache_id,
                adopted=False,
                reason="expired",
                expires_at=expires_at,
            )
            continue
        usable.append((expires_at, pos, cand))

    # 3. 可用块按到期时间降序、输入次序升序交给缓存感知规划：/plan 对重叠
    #    部分按输入顺序优先，故重叠字节优先使用更耐久的块。与
    #    /client-range-decision 一样统一走缓存感知路径，单个范围允许超过
    #    fetch_limit（会被拆到多个回源块）。
    usable.sort(key=lambda item: (-item[0], item[1]))
    plan_req = PlanRequest(
        object_length=req.object_length,
        ranges=req.ranges,
        max_gap=req.max_gap,
        fetch_limit=req.fetch_limit,
        cached_blocks=[
            CachedBlock(cache_id=cand.cache_id, start=cand.start, end=cand.end)
            for _, _, cand in usable
        ],
    )
    plan_result = plan_with_cache(plan_req)

    referenced = {cb.cache_id for cb in plan_result.cached_blocks}
    for expires_at, pos, cand in usable:
        adopted = cand.cache_id in referenced
        statuses[pos] = CacheReadCandidateStatus(
            index=pos,
            cache_id=cand.cache_id,
            adopted=adopted,
            reason="adopted" if adopted else "not_referenced",
            expires_at=expires_at,
        )

    return CacheReadPlanResponse(
        plan=plan_result,
        candidates=[statuses[i] for i in range(len(req.candidates))],
    )
