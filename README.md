# Range 回源规划与重组服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：

- `POST /plan`：规范化闭区间 / suffix 范围，可选接收已缓存块（`cached_blocks`）。
  先按输入顺序计算缓存的可用覆盖，再对未覆盖片段按最大间隙合并（绝不跨过缓存
  覆盖）、按单次回源上限拆块，返回回源块以及每个原范围在回源块 / 缓存块中的
  切片映射——一个范围可以由多个来源连续拼成。
- `POST /assemble`：回显 `/plan` 的 `blocks`、`mappings`（以及 `cached_blocks`），
  并分别按块提供 Base64 回源内容与缓存内容；服务严格校验来源 id、边界、缺块
  与重复块后，依据切片映射按原 ranges 顺序重组，返回每段范围的 Base64 内容与长度。
- `POST /range-response`：接收 `/assemble` 返回的 `object_length`、`ranges` 与
  媒体类型，生成可供客户端直接下载的 `206 Partial Content` 响应表示。单段直接
  输出范围字节并附 `Content-Type`、`Content-Length`、`Content-Range`；多段按
  ranges 下标顺序生成严格 CRLF 分帧的 `multipart/byteranges`，每段附
  `Content-Type` 与 `Content-Range`。服务先校验下标恰好覆盖 `0..N-1`、区间在
  对象内且每段 Base64 解码字节数等于声明长度；`Content-Length` 按最终二进制
  正文计算，boundary 随机生成且不会与正文分隔行冲突。
- `POST /verify-fetch-batch`：校验按 `/plan` 的 `blocks` 逐块回源得到的上游
  `206` 响应。接收 `blocks`、`object_length` 与按块编号提供的上游状态码、
  `Content-Range`、ETag、Base64 响应体；只接受闭区间与规划块及对象总长完全
  一致、响应体字节数严格相符的 206，缺块、重复块、多余块一律拒绝。所有块必须
  携带**同一个强 ETag**（拒绝弱标签），可选的 `expected_etag` 还必须与之全等，
  防止不同版本的分块混入。通过后按块编号返回可直接用于 `/assemble.contents`
  的数据与确认的 ETag；校验失败返回带块编号与原因的 `422`，且不回显响应体。
- `POST /multipart-parse`：解析上游对多区间 Range 请求返回的
  `multipart/byteranges` **完整响应体**（Base64 传输）。严格按 CRLF 切分每个
  part，校验 `Content-Range` 闭区间、part 字节长度、对象总长度/边界、
  `Content-Type` 与 part 唯一性；part 集合必须与期望的 interval/suffix ranges
  一一对应（缺段、多段、重复段、错误 boundary、头部注入、非法 Base64、范围
  不匹配均返回可定位的 `422`），然后按期望 ranges 顺序返回各段 Base64 内容。
- `POST /secure-request`：上述两个操作的**统一鉴权入口**。请求携带 `operation`
  （`plan` 或 `assemble`）、`payload`（原接口请求体）以及 `key_id` / `timestamp`
  / `nonce` / `signature`；服务按固定 JSON 规范化后用环境配置的 HMAC-SHA256
  密钥验签、限制时间偏差，并对 `key_id+nonce` 做有效窗口内防重放。验签通过后
  执行对应操作并**原样返回该操作的响应**。`/plan` 与 `/assemble` 行为不受影响。
- `POST /safe-download-name`：解析上游 `Content-Disposition` 响应头，返回可
  直接用作本地下载文件名的**单一安全名称**。优先采用符合 RFC 5987 的
  `filename*`（支持 UTF-8 / ISO-8859-1 百分号编码），无效或缺失时回退到传统
  `filename`；解码后剥离路径前缀（`/` 与 `\`）、控制字符及尾部点/空格，空名与
  `.` / `..` 等危险名称一律拒绝，无法得到有效名称时返回 `422`。
- `POST /client-range-decision`：对象代理的**前置决策入口**，把客户端的
  `Range` 与可选 `If-Range` 请求头转换为可执行决策。严格按 RFC 9110 解析多段
  闭区间、开放终点（`bytes=500-`）与 suffix（`bytes=-500`），单个不可满足段
  跳过、全部不可满足给出 `416`（`Content-Range: bytes */总长`）；`If-Range`
  与当前强 ETag / Last-Modified 不匹配（弱 ETag 永不强匹配）时忽略 Range
  返回完整对象的 `200` 决策；匹配时复用现有规划逻辑给出 `206` 决策与可直接
  交给 `/assemble` 的 `plan`。非法头值返回带 `loc` 的可定位 `422`。
- `POST /conditional-range-decision`：在 `/client-range-decision` 之上增加
  GET 前置条件评估，额外接收客户端 `If-None-Match` 与 `If-Modified-Since`。
  `If-None-Match` 支持 `*` 与逗号分隔的标签列表，按 **RFC 9110 弱比较**
  （剥掉双方 `W/` 前缀后逐字节全等）匹配当前强 ETag；`If-None-Match` 存在时
  完全忽略 `If-Modified-Since`，仅在前者缺席时才按 HTTP 日期判断是否未修改。
  条件命中返回 `304` 与当前验证器（不解析 Range、不生成回源计划），未命中则
  完整复用 `200`/`206`/`416` 决策，`206` 的 `plan` 仍可直接提交
  `/assemble`；非法条件头返回带 `loc` 的 `422`。
- `POST /cache-write-decision`：在 `/verify-fetch-batch` 验收单个 206 分块后，
  判断该块能否写入**共享缓存**。输入对象 URI、块区间、已确认强 ETag、原请求头
  与上游 `Cache-Control`/`Age`/`Vary`；按 HTTP 指令语法解析含引号的取值，遇
  `no-store`、`private`、`Vary: *` 或请求带 `Authorization` 而响应未显式允许
  共享（`must-revalidate`/`public`/`s-maxage`）时拒绝。可缓存时 `s-maxage`
  优先于 `max-age`，扣除 `Age` 得剩余秒数（过期即拒绝）；稳定缓存键包含 URI、
  区间、ETag 与 `Vary` 指定的原请求头值（字段名大小写不敏感）。非法头值返回
  带 `loc` 的 `422`。
- `POST /cache-read-plan`：`/cache-write-decision` 的**读取侧**。接收对象
  URI、总长度、当前强 ETag、当前请求头、查询时刻、目标 ranges 与规划参数，
  以及候选缓存块的 `cache_id`、闭区间、`cache_key`、`vary_headers`、
  `stored_at`、`remaining_seconds`；按写入侧相同的键算法用当前请求逐块重算
  缓存键，仅保留键匹配（版本与 Vary 变体一致）且 `stored_at +
  remaining_seconds` 严格晚于查询时刻的块；可用块按到期时间降序、输入次序
  升序交给缓存感知 `/plan`（重叠字节优先使用更耐久的块），返回可直接用于
  `/assemble` 的 `plan`，并逐块给出采用状态与未采用原因
  （`key_mismatch`/`expired`/`not_referenced`）。重复 `cache_id`、非法区间
  或时间字段返回可定位的 `422`。

## Docker 一键启动

需要 Docker 20.10+（支持 Compose v2）。在工作目录准备好 `.env`（可从
`.env.example` 复制；只需 `/plan`、`/assemble` 等无鉴权端点时也可留空），
再用**单条命令**构建并后台启动：

```
docker compose up --build -d
```

- 访问地址：http://127.0.0.1:8080 （服务监听容器内 8080 端口，Compose
  映射 `127.0.0.1:8080:8080`，仅绑定本机回环地址，不对局域网/公网暴露）
- 接口文档（Swagger UI）：http://127.0.0.1:8080/docs
- 健康检查：`curl http://127.0.0.1:8080/healthz` → `{"status":"ok"}`

`/secure-request` 的 `SECURE_REQUEST_KEYS` /
`SECURE_REQUEST_TIMESTAMP_SKEW` / `SECURE_REQUEST_NONCE_TTL` /
`SECURE_REQUEST_NONCE_CAPACITY` 均由 Compose 从同目录 `.env` 读取（默认值
见下节“配置”），无需修改 `docker-compose.yml`。

查看日志与停止：

```
docker compose logs -f
docker compose down
```

不带 `-d` 可在前台运行。

不使用 Compose 时也可以直接用 Docker：

```
docker build -t range-planner:latest .
docker run --rm -p 127.0.0.1:8080:8080 range-planner:latest
```

## 配置

`/plan`、`/assemble`、`/range-response` 与本服务其余端点均无需任何配置。
`/secure-request` 通过以下环境变量配置：

| 环境变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SECURE_REQUEST_KEYS` | 使用 `/secure-request` 时必需 | 空 | 逗号分隔的 `key_id=secret` 列表，`secret` 为原始 UTF-8 密钥字节（建议使用 32 字节随机值的 hex/base64 文本，避免出现 `,` 与 `=`） |
| `SECURE_REQUEST_TIMESTAMP_SKEW` | 否 | `300` | 请求 `timestamp` 与服务端时间允许的最大偏差（秒） |
| `SECURE_REQUEST_NONCE_TTL` | 否 | `600`（2 倍偏差） | 同一 `key_id+nonce` 的防重放保留窗口（秒），应不小于时间偏差 |
| `SECURE_REQUEST_NONCE_CAPACITY` | 否 | `10000` | 进程内 nonce 防重放表的容量上限（正整数）；**进行中占位与窗口内已成功 nonce 统一计数**，容量已满且无过期项可清理时返回 `503 nonce_capacity_exceeded`（附 `capacity`），不会驱逐任何未过期 nonce |

未设置 `SECURE_REQUEST_KEYS`（或为空）时，`/plan`、`/assemble`、
`/range-response` 等端点仍正常工作，但 `/secure-request` 返回
`503 keys_not_configured`。密钥配置格式非法（启动时解析失败）会导致服务
直接启动失败，避免带错误配置上线。

可复制 `.env.example` 为 `.env` 后修改；`docker compose` 会自动读取同目录
`.env`。也可以直接用 `-e` 注入：

```
docker run --rm -p 127.0.0.1:8080:8080 \
  -e SECURE_REQUEST_KEYS='demo-key=0123456789abcdef0123456789abcdef' \
  range-planner:latest
```

如需更改对外端口，修改 `docker-compose.yml` 中的端口映射
`127.0.0.1:8080:8080`（容器内固定监听 8080）。

## 接口

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /plan`

请求示例（不带缓存，行为与旧版一致）：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128
}
```

响应中的 `blocks` 为合并后的回源块（含可直接使用的 `range_header`），
`mappings` 为每个原范围（按 `index` 对应输入下标）在块中的切片映射。

#### 缓存感知规划（可选 `cached_blocks`）

如果调用方本地已缓存了对象的某些字节，可以通过 `cached_blocks` 声明，
每项为稳定 id（字符串或整数，请求内唯一）加闭区间：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128,
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

规划规则：

1. **可用覆盖**：多个缓存块相互重叠时按输入顺序优先——先出现的缓存块占据
   重叠部分，后出现的只获得尚未被占据的剩余区间；
2. **未覆盖片段合并**：各范围扣除缓存覆盖后得到未覆盖片段，间隙
   `<= max_gap` 的相邻片段合并为一个回源段，但合并**不得跨过缓存覆盖**
   （间隙中含有缓存字节时绝不合并，避免重复回源）；
3. **按上限拆块**：合并后的回源段按 `fetch_limit` 拆成若干回源块，每块
   不超过上限。因此提供 `cached_blocks` 后，单个范围允许超过
   `fetch_limit`（会被拆到多个回源块），不再报 `unsatisfiable`；
4. **切片来源**：`mappings[].slices[]` 用 `source` 标明来源——`"fetch"`
   （携带 `block_index`）或 `"cache"`（携带 `cache_id`）。一个范围的多个
   切片按对象偏移顺序连续覆盖整个范围，可以横跨缓存块与多个回源块。

上面的请求会得到 3 个回源块 `[0,39]`、`[60,99]`、`[240,255]`，其中范围 0
的切片依次为 `fetch(块0)` → `cache(cdn-a)` → `fetch(块1)`：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "slices": [
        {"source": "fetch", "block_index": 0, "cache_id": null,
         "block_start": 0, "block_end": 39, "offset_in_block": 0, "length": 40},
        {"source": "cache", "block_index": null, "cache_id": "cdn-a",
         "block_start": 40, "block_end": 59, "offset_in_block": 0, "length": 20},
        {"source": "fetch", "block_index": 1, "cache_id": null,
         "block_start": 60, "block_end": 99, "offset_in_block": 0, "length": 40}
      ]
    },
    {
      "index": 1,
      "type": "suffix",
      "start": 240,
      "end": 255,
      "slices": [
        {"source": "fetch", "block_index": 2, "cache_id": null,
         "block_start": 240, "block_end": 255, "offset_in_block": 0, "length": 16}
      ]
    }
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

响应中的 `cached_blocks` 只回显本次规划**实际引用到**的缓存块（未命中任何
范围的缓存块不会出现）。若所有范围都命中缓存，`blocks` 为空列表。

### `POST /assemble`

请求体把 `/plan` 的返回原样回显，并在 `contents` 中为**每个**回源块提供其
完整回源内容（标准 Base64，含 `=` 填充）；若规划引用了缓存块，还需回显
`cached_blocks` 并在 `cache_contents` 中为**每个**缓存块提供完整缓存内容：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJw=="},
    {"block_index": 1, "data_b64": "PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiYw=="},
    {"block_index": 2, "data_b64": "8PHy8/T19vf4+fr7/P3+/w=="}
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ],
  "cache_contents": [
    {"cache_id": "cdn-a", "data_b64": "KCkqKywtLi8wMTIzNDU2Nzg5Ojs="}
  ]
}
```

校验规则（任一不满足都返回 `422`）：

1. `blocks[].index` 唯一、区间合法、`length == end - start + 1` 且不越界；
2. `contents[].block_index` 唯一，且与 `blocks` 一一对应——缺块、重复提供、
   提供多余块都会被拒绝；
3. `cached_blocks[].cache_id` 唯一、区间合法且不越界；`cache_contents[].cache_id`
   唯一，且与 `cached_blocks` 一一对应——缺块、重复提供、提供未声明的 id
   都会被拒绝；
4. `data_b64` 必须是合法的标准 Base64，解码字节数必须等于该来源块的声明长度；
5. `mappings[].index` 必须恰好覆盖 `0..N-1`（对应原 ranges 顺序，不可重复或缺失）；
6. 每个切片的来源必须合法：`source=fetch` 携带 `block_index` 且引用存在的
   回源块，`source=cache` 携带 `cache_id` 且引用已声明的缓存块；切片记录的
   `block_start/block_end` 与来源块一致，`offset_in_block + length` 不得超出
   来源块长度；
7. 切片必须从范围起点开始连续拼接（可横跨多个来源），且总长度等于
   `end - start + 1`。

全部命中缓存时 `blocks` 与 `contents` 传空列表即可。成功响应按原 ranges
顺序（`mappings[].index`）返回每段内容：

```json
{
  "object_length": 256,
  "ranges": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "length": 100,
      "data_b64": "..."
    }
  ]
}
```

典型调用流程：先请求 `/plan`，按 `blocks[].range_header` 发起实际回源，
把每块响应体做 Base64 编码；命中缓存的部分直接从本地缓存读取对应
`cached_blocks` 的字节并做 Base64 编码；最后连同 `blocks`、`mappings`、
`cached_blocks` 一起提交给 `/assemble`，即可取回与原请求 ranges 一一对应
的字节内容。

### `POST /range-response`

拿到 `/assemble` 的重组结果后，用本端点生成**可直接下发给下载客户端的
206 Partial Content 响应表示**（状态码、头字段与二进制正文）。本端点不需要
任何环境变量配置。

请求字段（`ranges` 可把 `/assemble` 响应中的同名数组原样回显）：

| 字段 | 说明 |
| --- | --- |
| `object_length` | 对象总字节长度（回显 `/assemble` 的同名值） |
| `ranges` | `/assemble` 返回的范围列表（`index/type/start/end/length/data_b64`），至少 1 段 |
| `content_type` | 对象媒体类型，形如 `type/subtype`，可带参数（如 `text/plain; charset=utf-8`）；将原样写入单段响应或每个 multipart part 的 `Content-Type` |

服务先执行严格校验（任一不满足返回 `422`，且不回显 `data_b64`）：

1. `ranges[].index` 非负、不重复，且**恰好覆盖 `0..N-1`**（缺号、越号、
   乱序输入允许但输出按下标排序）；
2. 每个区间 `start <= end`、`0 <= start` 且 `end < object_length`，
   `length == end - start + 1`；
3. `data_b64` 必须是含正确填充的标准 Base64（`validate=True`，拒绝
   URL-safe 字母表与夹杂空白），**解码字节数必须等于声明长度**；
4. `content_type` 必须是合法 RFC 9110 媒体类型（参数值支持 token 或
   quoted-string），拒绝空值、非 ASCII 与 CR/LF（防止响应头注入）。

#### 单段：直接输出范围字节

只有一段时，正文就是该段的范围字节，响应头包含 `Content-Type`、
`Content-Length` 与 `Content-Range: bytes start-end/total`，
`media_type` 为 `"single"`、`boundary` 为 `null`：

```bash
curl -s -X POST http://127.0.0.1:8080/range-response \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "object_length": 11,
    "content_type": "text/plain; charset=utf-8",
    "ranges": [
      {"index": 0, "type": "interval", "start": 0, "end": 4,
       "length": 5, "data_b64": "aGVsbG8="}
    ]
  }'
```

```json
{
  "status_code": 206,
  "headers": {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": "5",
    "Content-Range": "bytes 0-4/11"
  },
  "content_length": 5,
  "media_type": "single",
  "boundary": null,
  "body_b64": "aGVsbG8="
}
```

#### 多段：按 ranges 下标顺序生成 multipart/byteranges

多段时生成 RFC 2046 风格、严格 CRLF 分帧的 `multipart/byteranges`
完整正文：以 `--boundary` 开头、`--boundary--` 收尾（无任何前导/拖尾
字节），每个 part 恰好携带 `Content-Type` 与
`Content-Range: bytes start-end/total` 两个头。`boundary` 每次随机
（`range-response-` 前缀 + 24 个十六进制字符），服务会保证它不与任何
正文分隔行冲突；`Content-Length` 是**最终二进制正文**（含边界行与 part
头）的真实字节数：

```bash
curl -s -X POST http://127.0.0.1:8080/range-response \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "object_length": 11,
    "content_type": "text/plain; charset=utf-8",
    "ranges": [
      {"index": 0, "type": "interval", "start": 0, "end": 4,
       "length": 5, "data_b64": "aGVsbG8="},
      {"index": 1, "type": "interval", "start": 6, "end": 10,
       "length": 5, "data_b64": "d29ybGQ="}
    ]
  }'
```

```json
{
  "status_code": 206,
  "headers": {
    "Content-Type": "multipart/byteranges; boundary=range-response-83af89f8eaacb5c0d53c5724",
    "Content-Length": "288"
  },
  "content_length": 288,
  "media_type": "multipart/byteranges",
  "boundary": "range-response-83af89f8eaacb5c0d53c5724",
  "body_b64": "LS1yYW5nZS1yZXNwb25zZS04M2FmODlmOGVhYWNiNWMwZDUzYzU3MjQNCkNvbnRlbnQtVHlwZTogdGV4dC9wbGFpbjsgY2hhcnNldD11dGYtOA0KQ29udGVudC1SYW5nZTogYnl0ZXMgMC00LzExDQoNCmhlbGxvDQotLXJhbmdlLXJlc3BvbnNlLTgzYWY4OWY4ZWFhY2I1YzBkNTNjNTcyNA0KQ29udGVudC1UeXBlOiB0ZXh0L3BsYWluOyBjaGFyc2V0PXV0Zi04DQpDb250ZW50LVJhbmdlOiBieXRlcyA2LTEwLzExDQoNCndvcmxkDQotLXJhbmdlLXJlc3BvbnNlLTgzYWY4OWY4ZWFhY2I1YzBkNTNjNTcyNC0t"
}
```

客户端只需把 `status_code`、`headers` 与 `body_b64` 解码出的字节原样
写入下载响应即可。`body_b64` 的分帧与 [`POST /multipart-parse`
](#post-multipart-parse) 接受的格式完全一致——可回交给该端点做逐字节
核对。完整流程（/plan → 模拟 206 回源 → /assemble → /range-response →
/multipart-parse 往返验证）见
[`examples/range_response_example.py`](examples/range_response_example.py)：

```
python examples/range_response_example.py
```

主要错误码（错误体统一为
`{"error": "<code>", "detail": [{"message": ..., "loc": ..., "range_index": ...}]}`）：

| error | 含义 |
| --- | --- |
| `invalid_range_index` | 下标为负、重复或未恰好覆盖 0..N-1 |
| `duplicate_range_index` | 相同范围下标在 ranges 中重复 |
| `invalid_range` | 区间非法（end 小于 start） |
| `range_out_of_bounds` | 区间超出对象边界 |
| `range_length_mismatch` | 声明长度与区间应有的字节数不符 |
| `invalid_base64` | `data_b64` 非法 Base64 或填充错误 |
| `body_length_mismatch` | Base64 解码字节数与声明长度不符 |
| `invalid_content_type` | `content_type` 不是合法媒体类型（含 CR/LF 注入、非 ASCII 等） |

### `POST /verify-fetch-batch`

在真正组装之前，用本端点对 `/plan` 规划的逐块回源结果做一次**严格的上游
响应验收**：确认每块都是 HTTP `206 Partial Content`、`Content-Range` 与
规划闭区间及对象总长一致、响应体字节数精确相符，并通过**强 ETag** 确认
所有分块来自同一个对象版本。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `object_length` | 对象总字节长度（回显 `/plan` 的同名值） |
| `blocks` | 回显 `/plan` 返回的 `blocks`（全部命中缓存时为 `[]`） |
| `responses` | 按块提供的上游响应要素列表，每项见下表 |
| `expected_etag` | 可选；调用方此前确认过的强 ETag，提供时全批块必须与之全等 |

`responses[]` 每项：

| 字段 | 说明 |
| --- | --- |
| `block_index` | 回源块编号，对应 `blocks[].index` |
| `status_code` | 上游 HTTP 状态码，**只有 206 可通过**（200/416 等一律拒绝） |
| `content_range` | 上游 `Content-Range` 头原值，必须严格形如 `bytes start-end/total`（不接受 `*`） |
| `etag` | 上游 `ETag` 头原值；必须存在且为**强 ETag**（不得以 `W/` 开头） |
| `body_b64` | 该块上游响应体的标准 Base64（含正确 `=` 填充） |

请求示例（沿用 `/assemble` 一节的三个块）：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "expected_etag": "\"v3-strong\"",
  "responses": [
    {"block_index": 0, "status_code": 206,
     "content_range": "bytes 0-39/256", "etag": "\"v3-strong\"",
     "body_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJw=="},
    {"block_index": 1, "status_code": 206,
     "content_range": "bytes 60-99/256", "etag": "\"v3-strong\"",
     "body_b64": "PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiYw=="},
    {"block_index": 2, "status_code": 206,
     "content_range": "bytes 240-255/256", "etag": "\"v3-strong\"",
     "body_b64": "8PHy8/T19vf4+fr7/P3+/w=="}
  ]
}
```

校验通过时返回的 `contents` 元素与 `/assemble` 的 `contents` 元素结构完全
相同，且已按 `block_index` 升序排列——**可原样放入 `/assemble` 请求体**；
`etag` 为全批确认一致的强 ETag（去首尾空白后的原值，含引号）：

```json
{
  "object_length": 256,
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJw=="},
    {"block_index": 1, "data_b64": "PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiYw=="},
    {"block_index": 2, "data_b64": "8PHy8/T19vf4+fr7/P3+/w=="}
  ],
  "etag": "\"v3-strong\""
}
```

`blocks` 为空（全部命中缓存、无需回源）时，`responses` 也传空列表，服务返回
`contents: []` 与 `etag: null`；此场景下不得传 `expected_etag`（没有任何
响应可供确认，返回 `422`）。

#### 严格校验规则

按以下顺序执行（任一不满足立即返回 `422`，不回显任何响应体字节或 Base64）：

1. `expected_etag`（若提供）自身必须是强 ETag：双引号包裹的 opaque-tag，
   不得以 `W/`/`w/` 开头，不得含空格、控制字符或裸引号；
2. 回显的 `blocks` 自身合法：编号非负且唯一、`start <= end`、
   `length == end - start + 1`、`end < object_length`；
3. 每个 `responses[]` 元素：
   - `block_index` 非负、在 `responses` 中不重复，且必须存在于 `blocks`
     （多余块 `unexpected_block`、重复块 `duplicate_response`）；
   - `status_code == 206`，否则 `non_206_status`——防止上游把整对象 `200`
     或错误页（`416`/`5xx` 等）当作分块内容混入；
   - `content_range` 严格匹配 `bytes <start>-<end>/<total>`（RFC 7233
     数字形式，拒绝 `*`、单位非 `bytes`、反转区间与夹带换行的头部注入）；
     `<total>` 必须等于 `object_length`，闭区间必须与规划块**逐端点相等**；
   - `body_b64` 为合法标准 Base64（`validate=True`，拒绝 URL-safe 字母表
     与夹杂空白），解码后字节数必须恰好等于块长度（即 `end - start + 1`）；
   - `etag` 必须存在且为强 ETag；全批所有块的 ETag 去除首尾 OWS 后必须
     **逐字节相同**，不同版本的分块不得混合重组；
4. 提供了 `expected_etag` 时，其与全批确认到的 ETag 必须全等；
5. 存在性核对：`blocks` 中每个编号都必须恰好出现一次响应，缺块统一报
   `missing_blocks`（响应附全部缺失编号 `missing: [...]`）。

主要错误码（错误体统一为
`{"error": "<code>", "detail": [{"message": ..., "loc": ..., "block_index": ...}]}`，
定位形如 `responses[2].content_range`）：

| error | 含义 |
| --- | --- |
| `non_206_status` | 上游状态码不是 206 |
| `invalid_content_range` | Content-Range 语法非法（含 `*`、反转区间、非 bytes 单位等） |
| `content_range_total_mismatch` | Content-Range 的 total 与对象总长不符 |
| `content_range_mismatch` | Content-Range 闭区间与规划块不一致 |
| `invalid_base64` | 响应体 Base64 非法或填充错误 |
| `body_length_mismatch` | 响应体解码字节数与块声明长度不符 |
| `missing_etag` | 响应缺少 ETag 头（或为空） |
| `weak_etag` | 响应携带弱 ETag（W/ 前缀） |
| `invalid_etag` | ETag 不是合法的强 opaque-tag |
| `etag_mismatch` | 块间强 ETag 不一致（不同对象版本混入） |
| `weak_expected_etag` | expected_etag 自身是弱标签 |
| `invalid_expected_etag` | expected_etag 语法非法 |
| `expected_etag_mismatch` | 批次 ETag 与 expected_etag 不一致（版本已变化） |
| `expected_etag_unverifiable` | blocks/responses 为空时无法确认 expected_etag |
| `missing_blocks` | 缺少规划块的上游响应（附 `missing` 编号列表） |
| `duplicate_response` | 同一块的上游响应被重复提供 |
| `unexpected_block` | 提供了 blocks 中不存在的多余块响应 |
| `duplicate_block` / `invalid_block` / `invalid_block_length` / `block_out_of_bounds` / `invalid_block_index` | 回显的 blocks 自身非法 |

仅依赖标准库的完整示例（/plan → 模拟上游 206 → /verify-fetch-batch →
/assemble 逐字节核对）见
[`examples/verify_fetch_batch_example.py`](examples/verify_fetch_batch_example.py)：

```
python examples/verify_fetch_batch_example.py
```

### `POST /multipart-parse`

当上游（或代理）直接返回多区间响应 `Content-Type: multipart/byteranges;
boundary=...` 时，可用本端点把**完整响应体**解析回与请求 ranges 一一对应的
字节段。请求字段：

| 字段 | 说明 |
| --- | --- |
| `object_length` | 对象总字节长度；每个 part 的 `Content-Range` total 必须与之相等 |
| `ranges` | 期望的范围列表（`interval` 闭区间或 `suffix`），同时决定输出顺序；规范化后区间不得重复 |
| `boundary` | 上游实际使用的 boundary 值（不含前导 `--`），仅允许 RFC 2046 bcharsnospace 字符 |
| `expected_content_type` | 可选；提供时每个 part 的 `Content-Type` 去首尾空白后必须与之完全一致（大小写不敏感） |
| `body_b64` | 上游完整 multipart 响应体的标准 Base64（含全部 boundary 行、part 头与正文） |

请求示例（对象 256 字节，期望 `bytes=0-99` 与末尾 16 字节；响应原文 317 字节）：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "boundary": "8K3mZ9QrTx2v",
  "expected_content_type": "application/octet-stream",
  "body_b64": "LS04SzNtWjlRclR4MnYNCkNvbnRlbnQtVHlwZTogYXBwbGljYXRpb24vb2N0ZXQtc3RyZWFtDQpDb250ZW50LVJhbmdlOiBieXRlcyAwLTk5LzI1Ng0KDQoAAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyAhIiMkJSYnKCkqKywtLi8wMTIzNDU2Nzg5Ojs8PT4/QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl9gYWJjDQotLThLM21aOVFyVHgydg0KQ29udGVudC1UeXBlOiBhcHBsaWNhdGlvbi9vY3RldC1zdHJlYW0NCkNvbnRlbnQtUmFuZ2U6IGJ5dGVzIDI0MC0yNTUvMjU2DQoNCvDx8vP09fb3+Pn6+/z9/v8NCi0tOEszbVo5UXJUeDJ2LS0="
}
```

成功时**按 `ranges` 输入顺序**（而非上游 part 顺序）返回各段，`suffix`
规范化为实际闭区间；`parts` 为解析到的 part 数量：

```json
{
  "object_length": 256,
  "boundary": "8K3mZ9QrTx2v",
  "parts": 2,
  "ranges": [
    {"index": 0, "type": "interval", "start": 0, "end": 99, "length": 100, "data_b64": "AAECAw..."},
    {"index": 1, "type": "suffix",   "start": 240, "end": 255, "length": 16, "data_b64": "8PHy8..."}
  ]
}
```

仅依赖标准库、可直接运行的模拟上游 + 调用 + 逐字节核对示例见
[`examples/multipart_parse_example.py`](examples/multipart_parse_example.py)：

```
python examples/multipart_parse_example.py
```

#### 严格解析与校验规则

分帧（RFC 2046 风格，但比 RFC 更严格，任何不合规结构都返回 `422`）：

1. 响应体必须以 `--boundary` 开头，**不允许前导 preamble/字节**；
2. 每个 boundary 行后必须紧跟 CRLF，**拒绝裸 LF**；part 头与正文之间必须是
   恰好一个 `CRLFCRLF`；
3. 必须以 `--boundary--` 结束，其后**不允许任何拖尾字节（epilogue）**；
   响应截断、找不到下一边界均拒绝；
4. `body_b64` 必须是含正确填充的标准 Base64（`validate=True`，拒绝 URL-safe
   字母表与夹杂的空白）。

每个 part 的头部：

1. 逐行按 CRLF 解析，头部名必须是合法 token，取值不得包含控制字符；
   **拒绝折行（obs-fold）、空行、缺少冒号的行**；
2. **只允许 `Content-Type` 与 `Content-Range` 两个头部，且各自恰好出现一次**；
   任何其他头部（如 `Content-Length`、`X-...`、`Set-Cookie` 等）一律按
   头部注入（`unexpected_header`）拒绝，重复头部为 `duplicate_header`；
3. `Content-Range` 必须严格形如 `bytes <start>-<end>/<total>`（total 为十进制
   整数，不接受 `*`）；`total == object_length`、`start <= end`、
   `0 <= start`、`end < total`；part 正文字节数必须恰好等于
   `end - start + 1`；
4. `Content-Type` 必须是合法的 `type/subtype`（参数值支持 token 或
   quoted-string）；若请求提供了 `expected_content_type` 则必须与之匹配
   （大小写不敏感，允许 OWS）。

part 集合与期望范围：

1. 期望 `ranges` 先规范化为闭区间（suffix 同 `/plan` 语义；空对象上的 suffix
   与越界 interval 返回 `422`），规范化后区间**不得重复**；
2. 每个 part 的区间必须命中某个期望区间：未命中的为多余段
   （`unexpected_part`），相同区间的 part 重复为 `duplicate_part`；
3. 全部解析完成后做存在性核对：缺少期望区间对应的 part 返回 `missing_part`
   （响应中给出所有缺失区间）。

所有错误均为 HTTP `422`，响应体为
`{"error": "<code>", "detail": [{"message": ..., "loc": ..., ...}]}`，其中
`loc` 精确定位，例如 `parts[1].content_range`、`parts[0].headers[2]`、
`ranges[0]`、`body_b64`，并在适用时附带 `part_index` / `range_index` /
`start` / `end`。主要错误码：

| error | 含义 |
| --- | --- |
| `invalid_base64` | `body_b64` 非法 Base64 或填充错误 |
| `bad_boundary` | 响应体未以 `--boundary` 开头（含前导字节/preamble） |
| `bad_boundary_line` | boundary 行后不是 CRLF（裸 LF 或其他字节） |
| `truncated_multipart` | 响应体截断，缺少边界行 |
| `trailing_bytes` | 结束边界之后存在拖尾字节/epilogue |
| `empty_multipart` | multipart 中没有任何 part |
| `missing_header_terminator` | part 缺少 `CRLFCRLF` 头/正文分隔 |
| `missing_headers` | part 的头部块为空 |
| `malformed_header` | 头部行格式非法（缺冒号、名称非法等） |
| `header_injection` | 头部折行、空行或取值含控制字符 |
| `unexpected_header` | 出现 Content-Type/Content-Range 之外的头部 |
| `duplicate_header` | 同一头部在 part 中重复出现 |
| `missing_content_range` / `missing_content_type` | 缺少必需头部 |
| `invalid_content_range` | Content-Range 格式或闭区间非法 |
| `invalid_content_type` | Content-Type 语法非法 |
| `content_type_mismatch` | 与 `expected_content_type` 不一致 |
| `object_length_mismatch` | Content-Range 的 total 与对象长度不符 |
| `part_out_of_bounds` | part 区间超出对象边界 |
| `part_length_mismatch` | part 正文字节数与 Content-Range 声明长度不符 |
| `duplicate_part` | 相同 Content-Range 区间的 part 重复 |
| `unexpected_part` | part 区间不在期望 ranges 中（多余段） |
| `missing_part` | 期望范围缺少对应 part（缺段） |
| `invalid_range` / `range_out_of_bounds` / `unsatisfiable_suffix` | 期望范围非法、越界或空对象 suffix |
| `duplicate_expected_range` | 期望 ranges 规范化后区间重复 |

### `POST /secure-request`

`/plan` 与 `/assemble` 的统一鉴权入口。`payload` 字段就是对应操作的原始
请求体（与直接调用 `/plan`、`/assemble` 完全一致）：

```json
{
  "operation": "plan",
  "payload": {
    "object_length": 256,
    "ranges": [
      {"type": "interval", "start": 0, "end": 99},
      {"type": "suffix", "last_n": 16}
    ],
    "max_gap": 8,
    "fetch_limit": 128
  },
  "key_id": "demo-key",
  "timestamp": 1758537600,
  "nonce": "7f3c9a1e0b644f0d8a2e5c1b9a7e0d33",
  "signature": "小写十六进制的 HMAC-SHA256"
}
```

成功后**原样返回** `operation` 对应操作的响应（与直接调用 `/plan` 或
`/assemble` 的 JSON 完全相同）。可运行的完整示例（签名 + 调用 plan + 回源
模拟 + assemble 重组校验）见
[`examples/secure_request_example.py`](examples/secure_request_example.py)：

```
python examples/secure_request_example.py plan
python examples/secure_request_example.py assemble
```

#### 签名算法

1. 取 `operation`、`payload`、`key_id`、`timestamp`、`nonce` 五个字段
   （**不含** `signature`）组成签名对象；
2. 固定 JSON 规范化（**签名双方必须完全一致**）：
   - 键按字典序递归排序；
   - 无任何多余空白（分隔符为 `,` 与 `:`）；
   - UTF-8 编码、非 ASCII 字符不转义（`ensure_ascii=false`）；
   - 不允许 `NaN` / `Infinity`；
3. 用 `SECURE_REQUEST_KEYS` 中 `key_id` 对应的密钥，对规范化字节串计算
   HMAC-SHA256，输出小写十六进制作为 `signature`。

Python 参考实现（标准库）：

```python
import hashlib, hmac, json, time, uuid

def canonical_json(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")

envelope = {
    "operation": "plan",
    "payload": payload,
    "key_id": "demo-key",
    "timestamp": int(time.time()),
    "nonce": uuid.uuid4().hex,
}
envelope["signature"] = hmac.new(
    b"0123456789abcdef0123456789abcdef",
    canonical_json(envelope),
    hashlib.sha256,
).hexdigest()
```

#### 服务端校验顺序

1. 服务端未配置任何密钥 → `503 keys_not_configured`；
2. `|server_time - timestamp|` 超过 `SECURE_REQUEST_TIMESTAMP_SKEW`
   → `401 timestamp_skew`（响应附带 `server_time` 与 `max_skew_seconds`）；
3. `key_id` 在密钥表中不存在 → `401 unknown_key_id`；
4. 重算签名并常量时间比较，不一致 → `401 invalid_signature`；
5. 在**同一把锁内**依次完成 nonce 处理：先清理所有已过期 nonce → 再识别
   重放 → 最后为新 nonce 预留名额：
   - 同一 `key_id+nonce` 仍在 `SECURE_REQUEST_NONCE_TTL` 窗口内
     → `409 nonce_replayed`（即使容量已满，重放仍优先返回 `409`）；
   - 防重放表达到 `SECURE_REQUEST_NONCE_CAPACITY`（进行中占位与已成功
     nonce 合计）且清理后仍无空位 → `503 nonce_capacity_exceeded`（附
     `capacity`），**不执行 payload、也不驱逐任何未过期 nonce**；
6. 校验 `payload` 并执行对应操作：字段错误返回 `422 invalid_request`
   （错误路径以 `payload.` 开头，如 `payload.ranges[0].end`），
   业务语义错误沿用 `/plan`、`/assemble` 的 `422` 错误结构。

只有**执行成功（HTTP 200）**的请求才会占用 nonce；验签通过但 payload
非法或业务校验失败的请求会回滚 nonce 占位，客户端修正后可用同一 nonce
重试。回滚只删除请求自己的占位：若该占位恰好已过期并被并发请求重新占用，
原有记录不会被误删。重放已成功的 nonce 返回 `409`。

### `POST /safe-download-name`

解析上游响应头 `Content-Disposition`，产出可安全写入本地文件系统的单一
文件名（不依赖任何环境变量，无需鉴权）。请求体只有一个字段：

| 字段 | 说明 |
| --- | --- |
| `content_disposition` | 上游 `Content-Disposition` 响应头的**原始值**（长度 1–8192 字符） |

调用示例（传统 `filename` 直接包含 UTF-8 名称，服务按 UTF-8 解码）：

```bash
curl -s -X POST http://127.0.0.1:8080/safe-download-name \
  -H 'Content-Type: application/json' \
  --data-raw '{"content_disposition": "attachment; filename=\"报告-2026.pdf\""}'
```

响应：

```json
{
  "filename": "报告-2026.pdf",
  "source": "filename"
}
```

使用 RFC 5987 `filename*`（百分号编码，优先级最高）时建议用代码构造请求
（shell 中单引号与 RFC 值里的 `charset'language'` 分隔符容易混淆）：

```python
import json, urllib.request

body = json.dumps({
    "content_disposition":
        "attachment; filename=\"report 2026.pdf\"; "
        "filename*=utf-8''%E6%8A%A5%E5%91%8A-2026.pdf"
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/safe-download-name",
    data=body, headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
# {"filename":"报告-2026.pdf","source":"filename*"}
```

成功响应（HTTP 200）中的 `source` 标明最终名称的来源：`"filename*"`
（RFC 5987 扩展参数）或 `"filename"`（传统回退参数）。

#### 解析与回退规则

1. 头值按 RFC 6266 风格解析：`<disposition-type> *( ";" 参数 )`，参数名大小写
   不敏感；支持 token 与 quoted-string 两种取值（含 `\"`、`\\` 等 quoted-pair
   转义）；`attachment` / `inline` 之外的其他 disposition type 也可解析。
2. **优先采用 `filename*`**：必须形如 `charset'language'value`——`charset` 仅
   接受 `UTF-8` 与 `ISO-8859-1`（大小写不敏感），`language` 可为空或符合
   language-tag 语法，`value` 由 attr-char 与严格的百分号字节（`%HH`）组成，
   还原后的字节必须能按声明字符集成功解码。
3. **回退到 `filename`**：`filename*` 缺失、字符集/百分号/语言标签/字节序列
   非法，或解码结果清理后为空时，改用传统 `filename`；取值按 UTF-8 优先、
   ISO-8859-1 兜底解码（兼容直接把 UTF-8 字节放进 quoted-string 的上游）。
   出现多个 `filename` 时取第一个。
4. 两者都无法得到有效名称时返回 `422`。

#### 文件名清理规则

解码后的候选名称依次经过以下处理，确保结果是不含路径信息的单一文件名：

1. 删除全部控制字符（`0x00–0x1F` 与 DEL `0x7F`，包括 NUL、BEL 等）；
2. 按 POSIX（`/`）与 Windows（`\`）分隔符切分，**只保留最后一段**，剥离任意
   目录前缀（如 `../../etc/passwd` → `passwd`、
   `C:\Windows\System32\drivers\etc\hosts` → `hosts`；百分号编码出来的
   `%2F` / `%5C` 同样生效，如 `..%2F..%2Fevil.sh` → `evil.sh`）；
3. 去除首尾空白，并循环剥离尾部的点与空格（Windows 资源管理器会忽略它们，
   可用于制造歧义文件名，如 `report.pdf...   ` → `report.pdf`）；
4. 结果为空、仅由空白组成，或仅由点构成（`.`、`..`、`...` 等）时视为危险
   空名，候选作废（`filename*` 作废后仍可回退到 `filename`；都无效则 `422`）。
   以点开头的正常隐藏文件名（如 `.bashrc`）不受影响。

示例对照：

| Content-Disposition 关键内容 | 200 响应 `filename` |
| --- | --- |
| `filename*=utf-8''%E4%B8%AD%E6%96%87.tar.gz` | `中文.tar.gz` |
| `filename*=iso-8859-1''caf%E9.txt` | `café.txt` |
| `filename=fallback.bin; filename*=utf-8''%zzbad`（filename* 非法→回退） | `fallback.bin` |
| `filename="../../etc/passwd"` | `passwd` |
| `filename="C:\\Windows\\System32\\hosts"` | `hosts` |
| `filename="report.pdf...   "` | `report.pdf` |
| `filename="a\x00b.txt"`（含 NUL） | `ab.txt` |
| `filename=".."` / `filename=""` / 无 filename | `422` |

无法得到有效名称时的错误响应：

```json
{"error": "no_safe_filename", "message": "Content-Disposition 中未提供有效的 filename* 或 filename，无法得到可安全使用的下载文件名"}
```

头值本身语法错误（折行 CR/LF、引号未闭合、参数缺少 `=`、取值既非 token 也非
quoted-string、disposition type 缺失等）返回
`{"error": "invalid_content_disposition", "message": ...}`。请求体字段缺失、
为空、超长或类型错误则与其他端点一致返回 `422 invalid_request`。

### `POST /client-range-decision`

收到客户端的条件范围请求后，用本端点把原始 `Range` / `If-Range` 头转换成
**对象代理可直接执行的决策**：该回完整对象（`200`）、部分内容（`206`，附带
可直接回源并交给 [`/assemble`](#post-assemble) 的 `/plan` 结果）还是
`416`。本端点不需要任何环境变量配置。

服务地址与端口与其他端点一致：`http://127.0.0.1:8080`（容器内固定监听
8080，Docker Compose 默认只绑定本机回环地址）。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `object_length` | 对象总字节长度 |
| `etag` | 对象当前的**强** ETag 头原值（如 `"\"v3\""`，含引号）；与 `last_modified` 至少提供一个，弱标签（`W/` 前缀）直接拒绝 |
| `last_modified` | 对象当前的 `Last-Modified` 头原值，必须是 IMF-fixdate（如 `Wed, 21 Oct 2015 07:28:00 GMT`） |
| `range_header` | 客户端 `Range` 头原值，仅支持 bytes 单位；省略或为 `null` 表示未携带 Range（决策 200） |
| `if_range` | 可选，客户端 `If-Range` 头原值：强 ETag 或 IMF-fixdate |
| `max_gap` / `fetch_limit` | 现有 `/plan` 的规划参数（含义相同） |
| `cached_blocks` | 可选，语义与 `/plan` 的 `cached_blocks` 完全一致 |

请求示例（单段闭区间）：

```bash
curl -s -X POST http://127.0.0.1:8080/client-range-decision \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "object_length": 256,
    "etag": "\"v3-strong\"",
    "range_header": "bytes=0-99",
    "max_gap": 8,
    "fetch_limit": 128
  }'
```

```json
{
  "status_code": 206,
  "reason": "partial",
  "headers": {
    "Content-Length": "100",
    "Content-Range": "bytes 0-99/256"
  },
  "object_length": 256,
  "media_type": "single",
  "content_length": 100,
  "segments": [
    {"index": 0, "kind": "closed", "requested_start": 0, "requested_end": 99,
     "suffix_length": null, "satisfiable": true, "start": 0, "end": 99,
     "plan_range_index": 0, "reason": "satisfied"}
  ],
  "satisfied_count": 1,
  "skipped_count": 0,
  "if_range_matched": true,
  "if_range_kind": null,
  "plan": {
    "object_length": 256,
    "blocks": [
      {"index": 0, "start": 0, "end": 99, "length": 100, "range_header": "bytes=0-99"}
    ],
    "mappings": [ ... ],
    "cached_blocks": []
  }
}
```

206 决策中的 `plan` 就是标准 `/plan` 响应（`blocks` / `mappings` /
`cached_blocks`）：代理按 `blocks[].range_header` 逐块回源后，把它与回源
内容原样提交给 `/assemble`，再用 `/range-response` 生成最终 206 响应。
完整的决策 → 回源 → 重组链路示例见
[`examples/client_range_decision_example.py`](examples/client_range_decision_example.py)：

```
python examples/client_range_decision_example.py
```

#### 决策规则

1. **无 Range（省略 / `null`）**：`status_code=200, reason=range_absent`，
   `headers` 只有 `Content-Length: <object_length>`，`plan` 为 `null`。
2. **携带 `If-Range` 时先做强比较**：
   - ETag 形式：必须与当前 `etag` **逐字节全等**才匹配；`W/` 弱标签即使取值
     相同也**不强匹配**（当前 ETag 自身是弱标签会被 422 拒绝，不会参与比较）；
   - 日期形式：当前 `last_modified` 早于或等于 `If-Range` 时刻才匹配
     （只接受 IMF-fixdate，并校验星期与日历日期一致）；
   - 不匹配（或没有对应类型的当前验证器可比较）时**忽略 Range**，
     `status_code=200, reason=if_range_mismatch`，`plan` 为 `null`。
3. **Range 解析（RFC 9110 bytes 单位，大小写不敏感）**：
   - 闭区间 `bytes=A-B`：`B` 超过对象尾端时收敛到尾端（如对象 256 字节时
     `bytes=250-9999` → `250-255`），仍可满足；
   - 开放终点 `bytes=A-`：取 `A` 到对象尾端；
   - suffix `bytes=-N`：取末尾 N 字节；N 大于对象长度时回落到整个对象；
   - 单个**不可满足段被跳过**（起点 `>= object_length` 的闭区间/开放终点、
     空对象上的 suffix），其余段照常 206；跳过信息见 `segments[].reason`
     （`skipped_start_past_end` / `skipped_empty_object_suffix`）与
     `skipped_count`；
   - **全部段不可满足**：`status_code=416, reason=unsatisfiable`，
     `headers` 恰好为 `{"Content-Range": "bytes */<object_length>"}`，
     `plan` 为 `null`。
4. **206 响应形式**：1 个可满足段时 `media_type="single"`，`headers` 含
   `Content-Length` 与该段的 `Content-Range`；多个可满足段时
   `media_type="multipart/byteranges"`（最终 `Content-Length` 取决于 multipart
   分帧，代理可用 `/range-response` 生成完整响应）。`content_length` 为各可满足
   段范围字节之和；`plan` 始终基于**可满足段**（其顺序与
   `segments[].plan_range_index` 对应），大范围超过 `fetch_limit` 时按现有
   缓存感知逻辑自动拆块。

多段 + 跳过示例（对象 256 字节，末段不可满足被跳过）：

```json
{
  "object_length": 256,
  "etag": "\"v3-strong\"",
  "range_header": "bytes=0-9, 240-, -16, 1000-2000",
  "max_gap": 8,
  "fetch_limit": 128
}
```

返回 `status_code=206, media_type="multipart/byteranges"`，
`satisfied_count=3`、`skipped_count=1`；`If-Range` 不匹配示例：

```bash
curl -s -X POST http://127.0.0.1:8080/client-range-decision \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "object_length": 256,
    "etag": "\"v3-strong\"",
    "range_header": "bytes=0-99",
    "if_range": "\"stale-version\"",
    "max_gap": 8,
    "fetch_limit": 128
  }'
# {"status_code":200,"reason":"if_range_mismatch",
#  "headers":{"Content-Length":"256"},"plan":null,"if_range_matched":false,...}
```

非法头值统一返回 `422`，响应体为
`{"error": "<code>", "detail": [{"message": ..., "loc": ..., "range_index": ...}]}`，
`loc` 精确定位：

| loc | 触发场景 |
| --- | --- |
| `range_header` | 非 bytes 单位、`bytes=` 后为空、空字符串/空白 Range、控制字符注入 |
| `range_header.ranges[i]` | 第 i 个范围表达式非法（反转区间、`-0`、数字内部空白、非数字等） |
| `if_range` | If-Range 既不是合法强 ETag 也不是合法 IMF-fixdate（含错误星期） |
| `etag` | 当前 ETag 为弱标签或不是合法强 opaque-tag |
| `last_modified` | 当前 Last-Modified 不是合法 IMF-fixdate |

主要错误码：`invalid_range_unit` / `invalid_range_header` /
`invalid_range_spec`（附带 `range_index`）/ `malformed_header`（CR/LF 等控制
字符）/ `weak_current_etag` / `invalid_current_etag` /
`invalid_if_range_etag` / `invalid_if_range_date` /
`invalid_last_modified` / `missing_etag`。请求体字段缺失或类型错误与其他
端点一致为 `422 invalid_request`（`etag` 与 `last_modified` 至少提供一个）。

### `POST /conditional-range-decision`

[`/client-range-decision`](#post-client-range-decision) 的**条件请求版本**：
在做 Range 决策之前先评估 GET 前置条件（RFC 9110 §13.2），决定直接回
`304 Not Modified` 还是继续正常的 `200`/`206`/`416` 决策。不需要任何环境变量
配置，服务地址同为 `http://127.0.0.1:8080`（Docker Compose 单命令启动：
`docker compose up --build -d`，默认只绑定本机回环地址）。

请求字段包含 `/client-range-decision` 的全部字段（`object_length`、`etag`、
`last_modified`、`range_header`、`if_range`、`max_gap`、`fetch_limit`、
`cached_blocks`），另加：

| 字段 | 说明 |
| --- | --- |
| `if_none_match` | 客户端 `If-None-Match` 头原值：`*` 或逗号分隔的 entity-tag 列表（每个标签形如 `"v1"` 或 `W/"v1"`，成员间允许 OWS）；省略/`null` 表示未携带 |
| `if_modified_since` | 客户端 `If-Modified-Since` 头原值，必须是 IMF-fixdate；**仅当 `if_none_match` 缺席时才会被评估** |

条件命中示例（即使携带 Range 也直接 304，不生成 `plan`）：

```bash
curl -s -X POST http://127.0.0.1:8080/conditional-range-decision \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "object_length": 256,
    "etag": "\"v3-strong\"",
    "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT",
    "range_header": "bytes=0-99",
    "if_none_match": "W/\"v3-strong\"",
    "max_gap": 8,
    "fetch_limit": 128
  }'
```

```json
{
  "status_code": 304,
  "reason": "not_modified",
  "headers": {"ETag": "\"v3-strong\"", "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
  "object_length": 256,
  "condition": {
    "condition_present": true,
    "matched": true,
    "matched_by": "if_none_match",
    "if_none_match_present": true,
    "if_none_match_kind": "list",
    "if_none_match_tags": ["W/\"v3-strong\""],
    "if_modified_since_evaluated": false
  },
  "decision": null
}
```

#### 条件评估规则

1. **`If-None-Match` 优先**：该头存在时按 RFC 9110 规则评估，并且**完全忽略
   `If-Modified-Since`**（连语法都不检查——条件未命中时一个非法 IMS 值不会
   导致 422；字段是否真的被评估见 `condition.if_modified_since_evaluated`）。
   - `*`：代理侧对象始终存在，**必然命中**；
   - 标签列表：以**弱比较**匹配当前强 ETag——剥掉双方可选的 `W/` 前缀后
     逐字节全等即命中，列表中**任一**标签命中即可（因此客户端持
     `W/"v3"` 也能匹配服务端强 `"v3"`）。当前对象没有 ETag 可比较时
     按未命中处理；
   - 命中后直接返回 `304`：不解析 `Range`/`If-Range`（其非法值不会报
     422），`headers` 携带当前 `ETag`/`Last-Modified` 验证器，
     `decision` 为 `null`。
2. **`If-None-Match` 缺席时才看 `If-Modified-Since`**：当前
   `Last-Modified` **早于或等于**条件时刻即视为未修改 → `304`；条件时刻
   更早（资源之后又改过）则未命中；没有当前 `last_modified` 可比较时同样
   按未命中处理。
3. **两个条件头都未携带**或条件未命中：完整复用
   [`/client-range-decision`](#post-client-range-decision) 的决策，结果原样放在
   `decision` 字段中（其 `plan`/`segments`/`headers` 等结构与单独调用完全
   一致），顶层 `status_code`/`reason`/`headers` 同步为该决策的值。
   `206` 的 `decision.plan` 就是标准 `/plan` 响应，可直接逐块回源后提交
   [`/assemble`](#post-assemble)（再用 [`/range-response`](#post-range-response)
   生成最终响应）。

未命中示例（旧标签 → 206，`decision.plan` 可回交 `/assemble`）：

```json
{
  "status_code": 206,
  "reason": "partial",
  "headers": {"Content-Length": "100", "Content-Range": "bytes 0-99/256"},
  "object_length": 256,
  "condition": {
    "condition_present": true,
    "matched": false,
    "matched_by": null,
    "if_none_match_present": true,
    "if_none_match_kind": "list",
    "if_none_match_tags": ["\"stale-version\""],
    "if_modified_since_evaluated": false
  },
  "decision": { "status_code": 206, "reason": "partial", "plan": { ... } }
}
```

非法条件头返回与 `/client-range-decision` 相同结构的 `422`，`loc` 标明字段：

| loc | 触发场景 |
| --- | --- |
| `if_none_match` | 空字符串/空白、裸文本（未加引号）、空成员或尾随逗号、`*` 混入列表、残缺引号、控制字符注入 |
| `if_modified_since` | 不是合法 IMF-fixdate（含星期与日历日期不一致、分量越界、RFC 850/asctime 废弃形式） |

主要错误码：`invalid_if_none_match` / `malformed_header`（CR/LF 等控制字符）
/ `invalid_if_modified_since`；当前 `etag` / `last_modified` / `range_header`
/ `if_range` 的错误定位规则与 `/client-range-decision` 完全相同。注意与
`/client-range-decision` 一样，`304`/`200`/`206`/`416` 都是**正常决策结果**
（HTTP 200 返回，状态码在响应体 `status_code` 中），只有头值非法/字段错误才是
HTTP `422`。

完整的条件命中 → 304、未命中 → 回源 → 重组链路示例见
[`examples/conditional_range_decision_example.py`](examples/conditional_range_decision_example.py)：

```
python examples/conditional_range_decision_example.py
```

### `POST /cache-write-decision`

在 [`/verify-fetch-batch`](#post-verify-fetch-batch) 验收单个 206 分块后，
用本端点判断该块能否写入**共享缓存**，并取得可缓存时的剩余新鲜秒数与
**稳定缓存键**。本端点不需要任何环境变量配置，服务地址同为
`http://127.0.0.1:8080`。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `uri` | 对象 URI（1–8192 字符，不得含控制字符；原样进入缓存键，不做规范化） |
| `start` / `end` | 分块闭区间字节偏移（`0 <= start <= end`，通常取自 `/verify-fetch-batch` 验收过的块） |
| `etag` | 已确认的**强** ETag 头原值（如 `"\"v3\""`，含引号）；弱标签（`W/` 前缀）与非法值返回 `422` |
| `request_headers` | 代理收到的原请求头 `{字段名: 值}`（可空）；字段名大小写不敏感，用于 `Authorization` 检查与 `Vary` 取值 |
| `cache_control` | 上游 `Cache-Control` 头原值；省略/`null` 表示未携带 |
| `age` | 上游 `Age` 头原值（delta-seconds）；省略/`null` 按 0 处理 |
| `vary` | 上游 `Vary` 头原值；省略/`null` 表示未携带 |

请求示例：

```bash
curl -s -X POST http://127.0.0.1:8080/cache-write-decision \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "uri": "https://objects.example.com/bucket/file.bin",
    "start": 0,
    "end": 99,
    "etag": "\"v3-strong\"",
    "request_headers": {"Accept-Encoding": "gzip"},
    "cache_control": "s-maxage=120, max-age=60, public",
    "age": "20",
    "vary": "Accept-Encoding"
  }'
```

```json
{
  "cacheable": true,
  "reason": "cacheable",
  "remaining_seconds": 100,
  "cache_key": "9c2e…（64 位十六进制）",
  "vary_headers": ["accept-encoding"]
}
```

#### 判定规则

先校验全部头值语法（任一非法即 `422`，先于任何判定），再按以下顺序判定
（命中即返回，`cacheable`/`reason` 都是 **HTTP 200 的正常判定结果**）：

1. **`no-store`**（RFC 9111 §5.2.2.5）：响应禁止存储 → `no_store`；
2. **`private`**（§5.2.2.7）：响应仅限私有缓存 → `private`（含
   `private="Set-Cookie"` 等限定字段形式）；
3. **`Vary: *`**（RFC 9110 §12.5.5）：无法构造可复用的缓存键 → `vary_star`
   （`*` 与其他成员混排时同样生效）；
4. **`Authorization`**（§3.5）：原请求携带 `Authorization`（字段名大小写
   不敏感）而响应 `Cache-Control` 未显式允许共享缓存——即不含
   `must-revalidate`、`public`、`s-maxage` 任一指令——
   → `authorization_not_shareable`；
5. **新鲜度**：`s-maxage` 优先于 `max-age`（§5.2.2.10）；两者皆无时不做
   启发式缓存 → `no_explicit_freshness`；`剩余秒数 = 新鲜度 - Age`，
   `<= 0` 即过期 → `expired`；
6. 以上都未命中 → `cacheable`，返回剩余秒数与缓存键。

`Cache-Control` 按 HTTP 指令语法解析：`#( token [ "=" ( token /
quoted-string ) ] )`——成员以逗号分隔（**引号内的逗号不分隔**），成员
前后及 `=` 周围允许 OWS/BWS，空成员按 RFC 9110 §5.6.1.1 忽略，
quoted-string 支持 `\"`、`\\` 等 quoted-pair 转义；指令名大小写不敏感。
`max-age`/`s-maxage` 的取值必须是 delta-seconds（token 或引号包裹形式均
可），同一指令重复出现按 `duplicate_cache_directive` 拒绝（共享缓存不得
猜测冲突的新鲜度语义）。`no-cache` 不阻止写入（仅要求使用时再验证）；
未知扩展指令一律忽略。

#### 缓存键构造

`cache_key` 是对以下规范化材料做固定 JSON 序列化（键按字典序排序、无多余
空白、UTF-8）后的 **SHA-256 十六进制**：

```json
{
  "uri": "<对象 URI 原值>",
  "start": 0,
  "end": 99,
  "etag": "<去首尾 OWS 后的强 ETag，含引号>",
  "vary": [["<小写字段名>", "<原请求头值 或 null>"], ...]
}
```

- `vary` 列表按 `Vary` 指定的字段名（小写、去重、字典序）排列，取值取自
  `request_headers`：字段名匹配**大小写不敏感**（大小写不同的同名头按
  接收顺序以 `", "` 合并），值仅去首尾 OWS、保留原大小写与内部空白；
  请求中缺失的头以 `null` 参与（与空字符串取值产生不同的键）；
- 因此**字段名大小写、Vary 成员的大小写与顺序、ETag 首尾空白都不影响
  缓存键**，而 URI、区间、ETag 或任一 Vary 取值变化都会得到不同的键；
- 不可缓存时 `cache_key` 与 `remaining_seconds` 均为 `null`。

响应中的 `vary_headers` 回显 `Vary` 解析出的小写字段名列表（无 `Vary`
或 `Vary: *` 时为空列表），便于核对缓存键成分。

#### 错误响应

非法头值统一返回 `422`，响应体为
`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，`loc`
精确定位（`uri` / `etag` / `cache_control` / `age` / `vary` /
`request_headers.<字段名>`）。主要错误码：

| error | 含义 |
| --- | --- |
| `invalid_uri` | `uri` 为空白串或含控制字符 |
| `missing_etag` | `etag` 为空白 |
| `weak_etag` | `etag` 为弱标签（`W/` 前缀） |
| `invalid_etag` | `etag` 不是合法的强 opaque-tag |
| `invalid_cache_control` | `Cache-Control` 语法非法（未闭合引号、非法 token、`max-age`/`s-maxage` 非数字或缺取值、空白/空指令列表等） |
| `duplicate_cache_directive` | `max-age` 或 `s-maxage` 重复出现 |
| `invalid_age` | `Age` 不是 delta-seconds（非负十进制整数） |
| `invalid_vary` | `Vary` 成员非法、为空白或不含任何有效成员 |
| `invalid_request_header` | 原请求头字段名非合法 token 或取值超长 |
| `malformed_header` | 头值含 CR/LF 等控制字符（拒绝折行与头部注入） |

字段缺失、类型错误（如 `end < start`、`start` 为负）与其他端点一致返回
`422 invalid_request`。

完整的 验收 → 逐块判定 → 缓存键稳定性演示示例见
[`examples/cache_write_decision_example.py`](examples/cache_write_decision_example.py)：

```
python examples/cache_write_decision_example.py
```

### `POST /cache-read-plan`

[`/cache-write-decision`](#post-cache-write-decision) 的**读取侧**：代理响应
新请求时，把缓存中记录的候选块元数据交给本端点，用**当前请求**的 URI、
强 ETag 与请求头按写入侧完全相同的键算法逐块重算缓存键，筛出仍然可用的
缓存块，并直接得到可用于 [`/assemble`](#post-assemble) 的回源规划。本端点
不需要任何环境变量配置，服务地址同为 `http://127.0.0.1:8080`（Docker
Compose 单命令启动：`docker compose up --build -d`，容器内固定监听 8080，
默认只绑定本机回环地址）。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `uri` | 对象 URI（必须与写入时一致；原样参与重算键，不做规范化） |
| `object_length` | 对象总字节长度 |
| `etag` | 对象**当前**的强 ETag 头原值（如 `"\"v3\""`，含引号）；弱标签（`W/` 前缀）与非法值返回 `422` |
| `request_headers` | 当前请求头 `{字段名: 值}`（可空）；字段名大小写不敏感，用于重算缓存键的 Vary 成分 |
| `now` | 查询时刻（Unix 秒，`>= 0`）；`stored_at + remaining_seconds` 必须**严格大于**它才可用 |
| `ranges` | 目标范围列表（`interval` 闭区间或 `suffix`），语义与 `/plan` 一致 |
| `max_gap` / `fetch_limit` | 现有 `/plan` 的规划参数（含义相同） |
| `candidates` | 候选缓存块列表（可为空），每项见下表 |

`candidates[]` 每项（即写入缓存时记录的元数据）：

| 字段 | 说明 |
| --- | --- |
| `cache_id` | 缓存块稳定 id（字符串或整数，请求内唯一，重复返回 `422`） |
| `start` / `end` | 缓存块闭区间字节偏移（`0 <= start <= end < object_length`） |
| `cache_key` | 写入时 `/cache-write-decision` 返回的稳定缓存键（64 位小写十六进制） |
| `vary_headers` | 写入时响应的 `vary_headers`（Vary 指定的字段名列表；无 Vary 时为 `[]`） |
| `stored_at` | 写入时刻（Unix 秒，`>= 0`） |
| `remaining_seconds` | 写入时判定的剩余新鲜秒数（`>= 1`） |

请求示例（三个候选块：一个可用、一个版本已变化、一个已过期）：

```bash
curl -s -X POST http://127.0.0.1:8080/cache-read-plan \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "uri": "https://objects.example.com/bucket/file.bin",
    "object_length": 256,
    "etag": "\"v3-strong\"",
    "request_headers": {"Accept-Encoding": "gzip"},
    "now": 1758700000,
    "ranges": [{"type": "interval", "start": 0, "end": 149}],
    "max_gap": 8,
    "fetch_limit": 128,
    "candidates": [
      {"cache_id": "cdn-a", "start": 0, "end": 99,
       "cache_key": "715cc9faf03ce6063b790a904e1922434abccdaf2874f1a1ac6cbc83cdcf2c82",
       "vary_headers": ["accept-encoding"],
       "stored_at": 1758699900, "remaining_seconds": 200},
      {"cache_id": "cdn-b", "start": 100, "end": 199,
       "cache_key": "74e9741800148b9e8e38506e1f9ebd968ff3840e59391dffeab59b9eecf8ea1b",
       "vary_headers": ["accept-encoding"],
       "stored_at": 1758699950, "remaining_seconds": 300},
      {"cache_id": "cdn-c", "start": 200, "end": 255,
       "cache_key": "744255e77c1567608cc8f76de02ac26e942a11b93fcbaf11bbb2fc8e77abee83",
       "vary_headers": [],
       "stored_at": 1758699800, "remaining_seconds": 100}
    ]
  }'
```

响应：`plan` 与 `/plan` 输出结构完全一致（可直接回源并提交 `/assemble`），
`candidates` 按输入顺序逐块给出采用状态与未采用原因：

```json
{
  "plan": {
    "object_length": 256,
    "blocks": [
      {"index": 0, "start": 100, "end": 149, "length": 50, "range_header": "bytes=100-149"}
    ],
    "mappings": [
      {"index": 0, "type": "interval", "start": 0, "end": 149,
       "slices": [
         {"source": "cache", "block_index": null, "cache_id": "cdn-a",
          "block_start": 0, "block_end": 99, "offset_in_block": 0, "length": 100},
         {"source": "fetch", "block_index": 0, "cache_id": null,
          "block_start": 100, "block_end": 149, "offset_in_block": 0, "length": 50}
       ]}
    ],
    "cached_blocks": [
      {"cache_id": "cdn-a", "start": 0, "end": 99}
    ]
  },
  "candidates": [
    {"index": 0, "cache_id": "cdn-a", "adopted": true,  "reason": "adopted",      "expires_at": 1758700100},
    {"index": 1, "cache_id": "cdn-b", "adopted": false, "reason": "key_mismatch", "expires_at": 1758700250},
    {"index": 2, "cache_id": "cdn-c", "adopted": false, "reason": "expired",      "expires_at": 1758699900}
  ]
}
```

上例中 `cdn-a` 的键是用 `"v3-strong"` 与 `Accept-Encoding: gzip` 算出的，
与当前请求一致且未过期，被采用；`cdn-b` 的键是用旧版本 `"v2-old"` 算出的，
重算不一致（`key_mismatch`）；`cdn-c` 在查询时刻刚好到期（`1758699800 +
100 = 1758699900 <= now`，相等即过期）。

#### 筛选与排序规则

1. **语法校验**（任一非法即 `422`，先于任何筛选）：`uri`/`etag`/
   `request_headers` 的校验与 `/cache-write-decision` 完全相同；逐块校验
   区间（`start <= end < object_length`）、`cache_key` 格式（64 位小写十六
   进制）、`vary_headers` 成员（合法字段名 token，不允许 `*`）与
   `cache_id` 唯一性；
2. **重算键**：对每个候选块，用当前 `uri`、候选块闭区间、当前 `etag`、
   候选块 `vary_headers`（小写、去重、排序规范化）与当前 `request_headers`
   按 [`/cache-write-decision` 的键算法](#post-cache-write-decision)重算，
   与候选 `cache_key` 全等才保留——键材料包含 ETag 与 Vary 取值，故**键
   匹配即对象版本一致且当前请求的 Vary 变体与写入时相同**；
3. **新鲜度**：`stored_at + remaining_seconds` **严格大于** `now` 才可用
   （相等即过期）；
4. **规划**：可用块按**到期时间（`stored_at + remaining_seconds`）降序、
   输入次序升序**作为 `cached_blocks` 交给缓存感知 `/plan`——`/plan` 对
   重叠部分按输入顺序优先，故相互重叠的字节优先由**更耐久**（到期更晚）
   的块提供；到期时间相同按输入次序。规划结果与 `/plan` 输出结构一致，
   可直接回源并提交 `/assemble`；与 `/client-range-decision` 一样，单个
   范围允许超过 `fetch_limit`（会被拆到多个回源块）。

`candidates[].reason` 取值：

| reason | 含义 |
| --- | --- |
| `adopted` | 键匹配且未过期，并被目标范围实际引用（出现在 `plan.cached_blocks`） |
| `not_referenced` | 键匹配且未过期，但不与任何目标范围相交，未进入 `plan` |
| `key_mismatch` | 按当前请求重算的键与 `cache_key` 不一致（URI/版本/Vary 变体不符） |
| `expired` | `stored_at + remaining_seconds` 不晚于 `now`（已过期） |

#### 错误响应

非法输入统一返回 `422`，响应体为
`{"error": "<code>", "detail": [{"message": ..., "loc": ..., "cache_id": ...}]}`，
`loc` 精确定位（如 `candidates[1].cache_id`、`candidates[0].cache_key`、
`etag`）。主要错误码：

| error | 含义 |
| --- | --- |
| `duplicate_cache_id` | `candidates` 中 `cache_id` 重复 |
| `invalid_interval` | 候选块区间非法（`end < start`） |
| `interval_out_of_bounds` | 候选块区间越界（`end >= object_length`） |
| `invalid_cache_key` | `cache_key` 不是 64 位小写十六进制 |
| `invalid_vary_headers` | `vary_headers` 成员非法（非字段名 token、含 `*`、含控制字符等） |
| `invalid_uri` / `missing_etag` / `weak_etag` / `invalid_etag` / `invalid_request_header` / `malformed_header` | 请求级头值错误，规则与 [`/cache-write-decision`](#post-cache-write-decision) 完全相同 |

字段缺失或类型错误（`stored_at` 为负、`remaining_seconds < 1`、`now` 为负、
`cache_id` 为布尔值等）与其他端点一致返回 `422 invalid_request`；目标范围
不可满足沿用 `/plan` 的 `unsatisfiable` 结构。

完整的 写入 → 读取 → 重组闭环示例见
[`examples/cache_read_plan_example.py`](examples/cache_read_plan_example.py)：

```
python examples/cache_read_plan_example.py
```

## 错误响应

业务请求/语义错误统一返回 HTTP `422`：

- 字段类型或约束错误：`{"error": "invalid_request", "detail": [...]}`；
  `/secure-request` 中 payload 字段错误的路径以 `payload` 开头；
- `/multipart-parse` 的分帧、头部、范围与 Base64 校验错误同样为 `422`，
  错误码与 `loc` 定位规则见上方 [`POST /multipart-parse`](#post-multipart-parse)
  一节；
- `/safe-download-name` 在头值语法错误时返回
  `{"error": "invalid_content_disposition", "message": ...}`，
  在头值合法但解析/清理后无任何可用名称时返回
  `{"error": "no_safe_filename", "message": ...}`；
- `/client-range-decision` 的 Range/If-Range/ETag/Last-Modified 头值错误采用
  `detail[0].loc` 可定位结构（`range_header`、`range_header.ranges[i]`、
  `if_range`、`etag`、`last_modified`，范围错误附 `range_index`）；注意
  `200`/`206`/`416` 是**正常决策结果**（HTTP 200 返回，状态码在响应体
  `status_code` 中），只有头值非法/字段错误才是 HTTP `422`，错误码与含义见
  [`POST /client-range-decision`](#post-client-range-decision) 一节；
- `/conditional-range-decision` 沿用同一错误结构，额外以
  `if_none_match` / `if_modified_since` 定位条件头错误（主要错误码
  `invalid_if_none_match` / `invalid_if_modified_since`）；`304` 同样是
  正常决策结果（HTTP 200 返回），见
  [`POST /conditional-range-decision`](#post-conditional-range-decision) 一节；
- `/cache-write-decision` 的头值错误采用 `detail[0].loc` 可定位结构
  （`uri`、`etag`、`cache_control`、`age`、`vary`、
  `request_headers.<字段名>`）；注意可缓存/不可缓存都是**正常判定结果**
  （HTTP 200 返回，结论在响应体 `cacheable`/`reason` 中），只有头值非法/
  字段错误才是 HTTP `422`，错误码与含义见
  [`POST /cache-write-decision`](#post-cache-write-decision) 一节；
- `/cache-read-plan` 的候选块与请求级头值错误采用同一结构
  （`detail[0]` 携带 `loc`，如 `candidates[1].cache_id`，并按需附带
  `cache_id`）；候选块未采用（`key_mismatch`/`expired`/`not_referenced`）
  是 **HTTP 200 的正常结果**（见响应体 `candidates`），只有重复
  `cache_id`、非法区间/时间字段、非法 `cache_key`/`vary_headers` 等才是
  HTTP `422`，错误码与含义见
  [`POST /cache-read-plan`](#post-cache-read-plan) 一节；
- `/plan` 范围不可满足：`{"error": "unsatisfiable", "message": ..., "range_index": ...}`；
- `/assemble` 组装错误：`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，
  其中 `loc` 指向出错的字段路径（如 `contents[2].data_b64`、
  `mappings[0].slices[1]`），并按需附带 `block_index` / `cache_id` /
  `range_index` / `slice_index`；
- `/verify-fetch-batch` 回源验收错误采用同一结构：`detail[0]` 携带
  `loc`（如 `responses[1].content_range`）与可归属的 `block_index`，
  `missing_blocks` 另附 `missing` 编号列表；错误响应**绝不回显响应体**或
  任何一方 ETag 的具体取值，错误码与含义见
  [`POST /verify-fetch-batch`](#post-verify-fetch-batch) 一节；主要错误码：

  | error                       | 含义                               |
  | --------------------------- | ---------------------------------- |
  | `missing_block`             | 缺少回源块内容                     |
  | `duplicate_block_content`   | 同一回源块内容被重复提供           |
  | `unexpected_block`          | 提供了 blocks 中不存在的回源块     |
  | `duplicate_block`           | blocks 中块编号重复                |
  | `missing_cache`             | 缺少缓存块内容                     |
  | `duplicate_cache_content`   | 同一缓存块内容被重复提供           |
  | `unexpected_cache`          | 提供了 cached_blocks 中未声明的 id |
  | `duplicate_cache`           | cached_blocks 中缓存 id 重复       |
  | `invalid_cache`             | 缓存块区间非法                     |
  | `cache_out_of_bounds`       | 缓存块超出对象长度                 |
  | `cache_length_mismatch`     | 缓存内容解码字节数与声明长度不符   |
  | `unknown_cache_reference`   | 切片引用了未声明的缓存块           |
  | `invalid_base64`            | Base64 非法或填充错误              |
  | `block_length_mismatch`     | 解码字节数与声明长度不符           |
  | `invalid_slice`             | 切片来源字段组合非法或偏移非法     |
  | `slice_out_of_bounds`       | 切片超出来源块边界                 |
  | `assembly_length_mismatch`  | 切片总长与范围声明长度不符         |
  | `slice_misaligned`          | 切片不能连续覆盖该范围             |
  | `unknown_block_reference`   | 切片引用了不存在的回源块           |
  | `slice_block_mismatch`      | 切片记录的区间与来源块声明不一致   |
  | `block_out_of_bounds`       | 回源块超出对象长度                 |
  | `duplicate_mapping_index`   | mappings 中下标重复                |
  | `invalid_mapping_index`     | mappings 下标未覆盖 0..N-1         |

- `/range-response` 的响应生成错误采用同一结构：`detail[0]` 携带 `loc`
  （如 `ranges[2].data_b64`、`content_type`）与可归属的 `range_index`，
  错误响应**绝不回显**任何 `data_b64`；错误码与含义见
  [`POST /range-response`](#post-range-response) 一节。

`/secure-request` 的鉴权类错误（均为 `{"error": ..., "message": ...}`）：

| HTTP | error                 | 含义                                              |
| ---- | --------------------- | ------------------------------------------------- |
| 401  | `timestamp_skew`      | 时间戳超出允许偏差（附 `server_time`、`max_skew_seconds`） |
| 401  | `unknown_key_id`      | 密钥表中不存在该 `key_id`（附 `key_id`）          |
| 401  | `invalid_signature`   | HMAC-SHA256 签名不符                              |
| 409  | `nonce_replayed`      | 同一 `key_id+nonce` 在有效窗口内已成功使用（附 `key_id`） |
| 503  | `keys_not_configured` | 服务端未配置 `SECURE_REQUEST_KEYS`，鉴权入口不可用 |
| 503  | `nonce_capacity_exceeded` | nonce 防重放表已达容量上限且无过期项可清理（附 `capacity`），不驱逐未过期 nonce、不执行 payload |

`/secure-request` 信封本身字段缺失或类型错误（如缺少 `signature`、
`timestamp` 不是整数）同样返回 `422 invalid_request`。

## 本地开发（不使用 Docker）

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

仅在需要调试 `/secure-request` 时才需导出密钥环境变量（例如
`export SECURE_REQUEST_KEYS='demo-key=0123456789abcdef0123456789abcdef'`）；
`/plan`、`/assemble`、`/range-response` 等端点无需任何配置。

运行测试（`TestClient` 随 `fastapi` 安装的 `starlette` 提供）：

```
python -m unittest discover -s tests
```
