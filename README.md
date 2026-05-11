# DIBR-self

多机位实时采集 → **mediamtx** 聚合/录制 → 渲染服务器结合 **头显回传的位姿**（Quest / PC OpenXR 等 XR 运行时提供）做新视点合成 → 双目画面经 **WebSocket** 推到头显或 PC 客户端。算法层目前为可替换的 Python stub，便于接入 DIBR 等模块。

## 仓库结构

| 目录 | 说明 |
|------|------|
| `computerA/` | 教室端：多路相机经 FFmpeg（优先 H.264 硬件编码）推 RTSP 到 mediamtx |
| `computerB-VR/` | 渲染端：拉多路 RTSP、跑算法 stub、WebSocket 与 VR 双向（位姿上行、双目 JPEG 下行） |
| `computerB-VR/quest_unity/Scripts/` | Unity 脚本示例：`DibrVrClient.cs`（Quest / OpenXR） |
| `computerB-VR/client/` | PC：`pc_pose_demo.py`（假位姿 + 桌面预览）；**`pc_openxr_client.py`（OpenXR 头显 + 同款 WebSocket 协议）** |

## 依赖

- **Python 3.10+**
- **FFmpeg**（`ffmpeg` 在 PATH 中；Windows 可装 [ffmpeg](https://ffmpeg.org/download.html)）
- **[mediamtx](https://github.com/bluenviron/mediamtx)**：部署在教室机、中心节点或渲染机均可，需为每路流配置 **`source: publisher`**（与下方 path 一致）
- 渲染端 Python 包见各目录 `requirements.txt`；PC OpenXR 客户端另见 **`requirements-pc-openxr.txt`（pyopenxr，勿与误装包 `openxr` 混淆）**
- Quest 端：**Unity** + XR 插件（Oculus / OpenXR），Android 构建需网络权限

### OpenXR 要装在哪一侧？

| 侧 | 是否需要 OpenXR |
|----|------------------|
| **渲染服务器（Python，`computerB-VR/server`）** | **不需要**。只解析 JSON 位姿与多路视频，与 OpenXR 无链接依赖。 |
| **PC 演示（`pc_pose_demo.py`）** | **不需要**。用程序生成的假位姿，仅用于协议联调。 |
| **PC 头显（`pc_openxr_client.py`）** | **需要**。依赖 **pyopenxr**（见 `requirements-pc-openxr.txt`）与本机 **OpenXR 运行时**（SteamVR OpenXR / WMR / Oculus PC 等）；勿装错 PyPI 包名 **`openxr`**。 |
| **Quest 实机（Unity）** | **需要 XR 运行时**。工程里通过 `UnityEngine.XR` 读头显位姿；在 Quest 上通常勾 **OpenXR** 或 **Oculus** 插件，由运行时提供追踪，脚本本身不直接 `using` OpenXR C API。 |
## 相机路数 K

- **采集**：`computerA/config.yaml` 里 `streams` 列表任意长度；每条唯一 `path`（如 `cam0` … `camK-1`）。
- **mediamtx**：`mediamtx.record.example.yml` 中为每个 path 复制一块配置，与采集 path 一致。
- **渲染**：`computerB-VR/config.yaml` 中要么写 **`stream_paths`** 列表，要么用 **`stream_count` + `stream_prefix`** 自动生成 `cam0`…`camN-1`（与 `stream_paths` 同时存在时优先列表）。

## 快速运行

### 1. mediamtx

将官方配置与 `computerA/mediamtx.record.example.yml` 中 `paths` 片段按需合并，保证存在 `cam0`、`cam1`… 等 publisher 路径，然后启动 mediamtx。

### 2. 教室端（ computerA ）

```powershell
cd computerA
pip install -r requirements.txt
copy config.example.yaml config.yaml
# 编辑 config.yaml：mediamtx_host、每路设备名/类型、path 与 mediamtx 一致
python publish_cameras.py -c config.yaml
```

### 3. 渲染端（ computerB-VR ）

```powershell
cd computerB-VR
pip install -r requirements.txt
copy config.example.yaml config.yaml
# 编辑 config.yaml：mediamtx_host、stream_paths 或 stream_count
python -m server.main -c config.yaml
```

默认 WebSocket：`ws://0.0.0.0:8765/`（本机访问可用 `ws://127.0.0.1:8765/`）。

### 4. PC 演示客户端（可选）

```powershell
cd computerB-VR
python client/pc_pose_demo.py -c config.yaml
# 或指定服务器：python client/pc_pose_demo.py -u ws://192.168.x.x:8765/
```

### 5. PC OpenXR 头显（可选）

```powershell
cd computerB-VR
pip uninstall openxr   # 若曾误装 drypy/openxr，请先卸掉
pip install -r requirements-pc-openxr.txt
python client/pc_openxr_client.py -c config.yaml
# 或: python client/pc_openxr_client.py -u ws://<渲染机IP>:8765/
```

可先启动 `server.main` 再戴头显运行；虚拟屏大小与距离可用 `--screen-height`、`--depth` 调节。

### 6. Quest（Unity）

将 `quest_unity/Scripts/DibrVrClient.cs` 拷入工程，场景中指定 `serverUrl`、左右眼 `RawImage`，构建 Android 到头显。

## 位姿与视频协议（简述）

- **上行（VR → 服务器）**：WebSocket **文本**，JSON，例如  
  `{"t_ns": ..., "pos": [x,y,z], "quat": [x,y,z,w]}`
- **下行（服务器 → VR）**：WebSocket **二进制**，`DIBR` 帧头 + 左右 JPEG 载荷；解析逻辑见 `computerB-VR/server/protocol.py` 与 `DibrVrClient.cs`

替换真实渲染时，改 `computerB-VR/server/algorithm_stub.py` 中 `render_stereo`（或改为调用你的算法模块）即可。

## 许可证

若未单独声明，以仓库内 LICENSE 为准；暂无 LICENSE 时仅内部使用。
