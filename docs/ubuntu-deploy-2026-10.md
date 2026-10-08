# Ubuntu 部署运行指南

从 Windows 开发机迁移到 Ubuntu（22.04 / 24.04 LTS，x86_64，桌面版或服务器版均可）
的完整过程。主流水线（一键渲染 / 局部编辑 / 8100 渲染 API）**不需要 GPU、不需要
Docker、不需要数据库**；只有旧主 API 栈（`run_api.py`，8000 端口）需要 PG/Valkey。

## 1. 系统基础依赖

```bash
sudo apt update
sudo apt install -y git curl build-essential ca-certificates
```

全部 Python 依赖都有 Linux 预编译轮子，正常不需要现场编译，`build-essential`
只是保险。**Python 3.12 不用手动装**——下一步的 uv 会按 `.python-version`
自动下载托管解释器。

## 2. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env    # 或重开终端
uv --version
```

## 3. 拉代码、装依赖

```bash
git clone <仓库地址> ai-render-pipeline
cd ai-render-pipeline
git checkout dev               # 稳定用 main
uv sync                        # 按 uv.lock 精确安装，缺 Python 3.12 会自动装
```

**关于 torch（唯一的大体积依赖）**：`pyproject.toml` 主依赖里有 torch 且钉了
cu121 源，x86_64 Linux 上会连带下载 nvidia 系列 wheel（约 3~4 GB 磁盘）。
实际上只有 CubiCasa 分割实验路线（`app/tools/cubicasa_segmenter.py`）真正
import torch，主流水线运行时用不到；无 GPU 机器照样能跑（代码里
`torch.cuda.is_available()` 自动回落 CPU）。想省磁盘/流量可在 `pyproject.toml`
里注释掉 torch 行后 `uv lock && uv sync`，注意这会使 lock 与 Windows 机器不一致。

## 4. ODA File Converter（只有输入 DWG 才需要；DXF/PNG/JPG 跳过本节）

1. 到 [OpenDesign](https://www.opendesign.com/guestfiles/oda_file_converter)
   下载 **Linux .deb** 版（与 Windows 侧 27.x 同代即可），安装并自动补齐 Qt 依赖：

   ```bash
   sudo apt install ./ODAFileConverter*.deb
   dpkg -L odafileconverter | grep -i bin    # 找到可执行文件实际路径
   ```

2. **有桌面环境**：装完二进制通常已进 PATH（默认配置 `oda_exe="ODAFileConverter"`
   直接命中），无需额外设置。
3. **服务器无显示器（headless）**：Linux 版 ODAFileConverter 是 Qt GUI 程序，
   直接跑会因缺 X display 报错。装 xvfb 并包一层 wrapper：

   ```bash
   sudo apt install -y xvfb
   sudo tee /usr/local/bin/oda-headless >/dev/null <<'EOF'
   #!/usr/bin/env bash
   exec xvfb-run -a ODAFileConverter "$@"
   EOF
   sudo chmod +x /usr/local/bin/oda-headless
   ```

   然后在 `.env` 里设 `ARP_ODA_EXE=/usr/local/bin/oda-headless`。

验证：`uv run python -c "import asyncio; from app.tools.cad.convert import convert_dwg; print(asyncio.run(convert_dwg('某个.dwg', '/tmp/_t')).ok)"`。

## 5. 配置 .env

```bash
cp .env.example .env
```

主流水线必填四项：

```dotenv
ARP_LLM_BASE_URL=https://openrouter.ai/api/v1
ARP_LLM_MODEL=qwen/qwen3.8-flash
ARP_LLM_API_KEY=<你的 OpenRouter key>
ARP_VISION_MODEL=qwen/qwen3.8-flash
```

**从 Windows 机器直接拷 `.env` 时必须改的两处**（key 本体直接 scp/手动抄过来即可，
不要提交进 git）：

| 条目 | Windows 值 | Ubuntu 处理 |
|---|---|---|
| `ARP_ODA_EXE` | `D:/program/ODA/.../ODAFileConverter.exe` | 按上节设 Linux 路径或 wrapper；不用 DWG 可删掉 |
| `ARP_BLENDER_EXE` | `C:\Program Files\...\blender.exe` | 仅旧主 API 栈用到：`sudo apt install blender` 后设 `/usr/bin/blender`；渲染 API 用不到，可删 |

其余条目（`ARP_COMFY_URL`、`ARP_PG_DSN`、`ARP_VALKEY_URL` 等）主流水线不读，保持默认。

## 6. 验证运行

### CLI 一键渲染

```bash
uv run python scripts/render_e2e.py --input fixtures/cad/01-平面系统图.dwg --force
```

产物在 `output/<日期>/<运行时分秒>/final.png`。中文文件名在 ext4 上没有 Windows
侧的编码问题，可放心使用。

### HTTP API（8100）

```bash
uv run python scripts/run_render_api.py          # 前台
# 或后台常驻：
nohup uv run python scripts/run_render_api.py >/dev/null 2>&1 &
```

另开终端验证：

```bash
curl http://localhost:8100/api/v1/healthz
curl -X POST http://localhost:8100/api/v1/renders \
  -F "file=@/tmp/plan.dxf" -F "force=true"       # 返回 job_id 后轮询 /api/v1/jobs/<id>
```

对外提供服务时放行端口：`sudo ufw allow 8100/tcp`。

## 7. 旧主 API 栈（可选，8000 端口，需 PG/Valkey）

```bash
# Docker（机器上没有的话）
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && exit   # 重新登录生效
docker compose -f deploy/docker-compose.services.yml up -d
```

`.env` 里 PG 的 DSN 注意写法差异：SQLAlchemy 用
`ARP_PG_DSN=postgresql+psycopg://arp:arp@localhost:5432/arp`。然后
`uv run python scripts/run_api.py`。Blender 相关功能需按第 5 节装好 Blender。

## 8. 常见问题

| 现象 | 原因/处理 |
|---|---|
| DWG 报 `ODA_MISSING` | ODA 未装或不在 PATH，按第 4 节设 `ARP_ODA_EXE` |
| ODA 报 Qt / `cannot open display` | headless 环境缺 X，用 xvfb wrapper（第 4 节第 3 步） |
| `uv sync` 很慢、占数 GB | torch cu121 CUDA 轮子，见第 3 节说明；无 GPU 可注释掉 |
| curl 上传失败/multipart 报错 | 中文文件名兼容性问题，上传用 ASCII 文件名 |
| Windows 上的历史产物找不到 | `output/` 不进 git，迁移需另行 scp |
