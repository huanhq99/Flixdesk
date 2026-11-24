# Flixdesk

Flixdesk 是一个可完全私有化部署的跨平台开源远程桌面项目（客户端 + 信令/中继服务端一体化仓库），基于开源项目 [CrossDesk](https://github.com/kunkundi/crossdesk) 与 [CrossDesk Server](https://github.com/kunkundi/crossdesk-server) 构建。

## 仓库结构

```text
Flixdesk/
├── client/                  # 跨平台客户端（Desktop / CLI / Android / iOS / Harmony / MCP）
│   ├── apps/                # 各平台客户端源码
│   ├── libs/                # 核心控制、终端、协议库
│   └── deps/submodules/minirtc  # MiniRTC 实时传输引擎（Git Submodule）
├── server/                  # 自托管服务端（WSS 信令 + SQLite + Web 后台 + Coturn 中继）
│   ├── compose.yaml         # Docker Compose 一键编排
│   ├── .env.example         # 环境变量模板
│   └── docker/              # Dockerfile、证书生成与启动脚本
└── scripts/                 # Flixdesk 辅助配置脚本
    ├── init-server-env.sh   # 自动生成 server/.env 及安全密钥
    └── set-default-server.sh # 一键替换客户端默认信令服务器地址
```

---

## 一、部署自己的服务端（Linux + Docker Compose）

### 1. 准备工作
- 一台拥有公网 IP 的 Linux 服务器（推荐 Ubuntu 22.04+），已安装 Docker 与 Docker Compose。
- 在云服务器安全组和防火墙放行以下端口：
  - `9099/TCP`：WSS 信令、HTTPS 接口及 Web 管理后台
  - `3478/TCP` & `3478/UDP`：STUN / TURN 服务
  - `50000–60000/UDP`：TURN 媒体中继端口

### 2. 生成服务端配置文件

在仓库根目录执行：

```bash
./scripts/init-server-env.sh --external-ip <你的公网IP> --internal-ip <本机内网IP>
```

可选参数：
- `--domain <你的域名>`：写入 `COTURN_PUBLIC_HOST`（使用自有域名时建议配置）
- `--port <端口>`：信令服务端口，默认 `9099`
- `--admin-user <用户名> --admin-pass <密码>`：启用 `/admin` Web 管理后台

脚本会自动调用 `openssl rand -hex 32` 生成 `COTURN_AUTH_SECRET` 与 `COTURN_STATELESS_NONCE_SECRET` 并写入 `server/.env`（已被 `.gitignore` 忽略，不会提交到 Git）。

### 3. 启动服务端

```bash
cd server
# 从本仓库源码构建并启动（推荐，完全独立于上游镜像）
docker compose up -d --build

# 查看运行状态
docker compose ps
```

### 4. TLS 证书说明

服务端约定读取以下证书文件名（位于 `${CROSSDESK_DATA_DIR}/certs/`，默认 `/var/lib/crossdesk/certs/`）：
- `api.crossdesk.cn_bundle.crt`：服务端证书链
- `api.crossdesk.cn.key`：服务端私钥
- `api.crossdesk.cn_root.crt`：自动生成的根证书（仅在使用自签证书时）

> **注意**：这只是服务端程序约定的**文件名**，不要求使用 `api.crossdesk.cn` 域名。
> - **使用公网 IP 直接部署**：首次启动会自动为 `EXTERNAL_IP` 生成自签证书，需要将 `api.crossdesk.cn_root.crt` 分发并导入各客户端的系统信任库（详见 `server/docs/DEPLOYMENT.md`）。
> - **使用自有域名部署（推荐）**：用 Let's Encrypt / 厂商证书签发你自己的域名证书，将证书链与私钥分别放到上述 `api.crossdesk.cn_bundle.crt` 和 `api.crossdesk.cn.key` 路径即可，客户端无需手动导入根证书。

---

## 二、切换客户端默认服务器（脱离官方服务器）

默认情况下，客户端源码中预置的信令服务器为 `api.crossdesk.cn:9099`。你可以在仓库根目录一键替换成你自己的域名或 IP：

```bash
./scripts/set-default-server.sh --host <你的域名或IP> --port 9099
```

如需同时替换客户端内置的官网链接、版本检查地址和下载地址，可附加参数：

```bash
./scripts/set-default-server.sh \
  --host api.example.com \
  --port 9099 \
  --website https://example.com \
  --version-origin https://version.example.com \
  --download-origin https://downloads.example.com
```

### 客户端核心硬编码位置速查
| 模块 | 文件路径 | 用途 |
| :--- | :--- | :--- |
| Desktop 默认配置 | `client/apps/desktop/src/config_center/config_center.h` | 默认信令域名 |
| Desktop 运行时状态 | `client/apps/desktop/src/gui/runtime/runtime_state.h` | 默认信令地址缓存 |
| Controller 公共库 | `client/libs/controller/include/controller_session.h` | 会话默认主机 |
| Android 客户端 | `client/apps/android/app/src/main/java/cn/crossdesk/mobile/MainActivity.java` | 默认服务器地址与端口 |
| iOS 客户端 | `client/apps/ios/CrossDeskMobile/RemoteSessionModel.swift` | 默认服务器地址与端口 |
| HarmonyOS 客户端 | `client/apps/harmony/entry/src/main/ets/mobile/MobileController.ets` | 默认服务器地址 |
| MCP 客户端 | `client/apps/mcp/src/store.ts`、`client/apps/mcp/ui/viewer.ts` | 默认服务器地址 |
| 自动更新检查 | `client/apps/desktop/src/version_checker/version_checker.cpp`、`client/apps/desktop/src/updater/update_core.cpp` | 版本清单与下载源 |

---

## 三、本地克隆与构建提示

克隆本仓库时请一并拉取 `minirtc` 子模块：

```bash
git clone --recursive <你的仓库地址>
```

客户端（Desktop）使用 [Xmake](https://xmake.io/) 构建，详细依赖与各平台编译命令请参考 `client/README_ZH.md`；服务端构建与生产运维说明请参考 `server/README.md` 与 `server/docs/DEPLOYMENT.md`。

---

## 开源协议与致谢

本项目基于以下上游开源项目衍生，严格保留原有开源协议与版权声明：
- **客户端（`client/`）**：衍生自 [`kunkundi/crossdesk`](https://github.com/kunkundi/crossdesk)（commit `c3916910653a093588c8a85ca3b6f886720edd49`），遵循 **GNU General Public License v3.0 (GPL-3.0-only)**，详见 `client/LICENSE` 与根目录 `LICENSE`。
- **服务端（`server/`）**：衍生自 [`kunkundi/crossdesk-server`](https://github.com/kunkundi/crossdesk-server)（commit `2d554408dfd7c5c2fba945292913028d60749f8d`），遵循 **GNU Lesser General Public License v3.0 (LGPL-3.0)**，详见 `server/LICENSE`。
- **传输引擎（`client/deps/submodules/minirtc`）**：引用 [`kunkundi/minirtc`](https://github.com/kunkundi/minirtc)（commit `5d4f24c42831234035be3c79cd31467753e06609`）。
