# redroid patches 与共享副屏状态栏组件

本 Fork 在上游 redroid 补丁之外，提供固定 Android 15 镜像的 **SystemUI 组件补丁＋状态栏宿主 APK** 构建入口。它直接校验并修改固定版本的 SystemUI APK，再生成派生镜像的构建上下文，不需要同步或编译整个 AOSP。

入口包含固定输入和工具链、APK 校验、实际派生镜像构建，以及隔离设备上的双屏与十会话验收。宿主仍是原型：依赖 Activity 存活，时钟和图标为静态素材，仅支持统一的图标明暗；尚未实现进程恢复、显示屏移除/重建、状态重放与旋转适配，也没有通知下拉或导航栏。镜像启动后需要显式启动宿主并绑定副屏；进程重启不会恢复窗口，显示屏销毁也不会自动清除宿主记录。

## 目录边界

| 位置 | 用途 | Git 管理 |
| --- | --- | --- |
| `~/dev/github/redroid-patches` | 补丁工具、宿主源码、版本锁、构建入口和回归 | 是 |
| `~/dev/build/redroid-android15/inputs` | 经摘要验证的原始 SystemUI APK | 否 |
| 同一工作区的 `cache` | 三个固定 Maven JAR，可复用本机 SDK 中的相同文件 | 否 |
| 同一工作区的 `artifacts/<name>` | 两个 APK、中间文件、证明文件和镜像上下文 | 否 |

工作区必须独立于源码仓库；`prepare` 只接纳空目录，使用锁摘要标识归属，重复运行不会清空内容。修改版本锁后需要另选空工作区。构建名不可覆盖，失败产物保留以便定位，下次使用新名字。

上游 `android-*` 和 `apply-patch.sh` 保留原用途；此组件入口不调用它们。

## 固定输入与验证

所有基线值只有一个来源：[组件版本锁](configs/android15-components.lock.json)。支持 Linux x86_64、Python 3.11+、锁定小版本的 JDK 17、Android SDK platform 35 与 build-tools 35.0.0；SDK 工具、android.jar 和三个 Maven JAR 逐个校验 SHA-256。JDK 编译目标为 Java 11，实际编译器记录在构建证明中。

SystemUI 输入必须同时匹配原 APK 摘要和平台证书。补丁仅在 `CommandQueue.onSystemBarAttributesChanged` 入口增加一次 bridge 调用，保留原回调；追加 bridge DEX，不替换全局 `IStatusBar`。接收端同时检查签名权限、共享的真实发送包/UID 和平台签名。验证覆盖：

- DEX039 的 magic、112 字节 header、file_size，以及 Android `dexdump` 解析。
- 被修改 DEX 的全部类、字段、方法、指令与 try/catch 语义，只有指定方法允许增加前缀调用；调试信息不在语义检查范围内，其余原 DEX 按字节核验。
- 原 APK 的非目标 ZIP 内容（含 manifest/resources）保持字节一致，新增内容只允许 bridge DEX 与签名。
- 两个输出 APK 的签名证书、16 KiB 页对齐；宿主包名及 SDK 级别。

签名材料从外部路径传入，不复制到构建上下文，也不进入 Git。固定基础镜像使用 AOSP 的公开开发测试平台证书；需要与它匹配的开发测试密钥，任意新建 debug keystore 均不适用。更换基础镜像或签名不能只改一个 tag，必须重新提取、锁定并验证完整输入合同。

## 使用

在本仓库执行；示例复用已有 SDK/JDK，按机器实际路径调整：

```bash
./build.sh doctor
./build.sh prepare
./build.sh deps --sdk /opt/buildploy/android-sdk

# 任选其一：导入已有的相同 APK，或从固定镜像提取。
./build.sh extract --source-apk /absolute/path/original-SystemUI.apk
# ./build.sh extract

./build.sh components --name prototype-1 \
  --sdk /opt/buildploy/android-sdk \
  --java-home /absolute/path/to/jdk-17 \
  --key /external/signing/platform.pk8 \
  --cert /external/signing/platform.x509.pem

# 校验产物与当前源码后，展示镜像命令；此命令不会构建镜像。
./build.sh image --name prototype-1 --tag redroid-statusbar:prototype-1 --dry-run

# 后续需要实际构建时，显式去掉 --dry-run。
# ./build.sh image --name prototype-1 --tag redroid-statusbar:prototype-1
```

每个命令可加 `--workspace /absolute/path`。`prepare/deps/extract/components --dry-run` 只输出计划，不写工作区、不访问 Docker、不下载。`image --dry-run` 需要已有成功的组件构建，并实际复核来源与产物哈希。

`extract` 没有 `--source-apk` 时会拉取锁定的基础镜像，通过本次创建的**未启动容器**复制 APK，并删除该临时容器；不会清理其他容器或镜像。`image` 使用不可变基础镜像摘要，只复制两个 APK，要求明确的非 `latest` tag；不会推送镜像、切换服务默认镜像或修改运行中的容器。基础镜像和新镜像的磁盘占用在 Docker 引擎所在盘，可能与本工作区不是同一块盘。

构建上下文中的 APK 固定为 `0644` 并在镜像入口校验，镜像内使用 root 所有者；兼容旧 Docker 构建器，不要求安装 BuildKit。

组件输出的 `build.json` 记录输入锁、源码摘要、工具版本、APK 验证、构建耗时和磁盘增量；`image/` 仅含两个 APK、Dockerfile 与 dockerignore。源码、锁或上下文变化后，旧构建不能用于 `image`，需重新构建。

## 空间与验证范围

此路线不再需要 AOSP 的数百 GB 工作区。组件操作预留 2 GiB 可用空间；这是构建余量门槛，不是实测占用。`doctor` 同时报告 WSL 文件系统容量和对应 Windows 宿主盘实际空闲容量；Windows 盘查询失败时状态为未知，实际构建会停止，不能拿 VHD 内的虚拟容量代替。

首次拉取基础镜像、Docker 层和 SDK/JDK 安装空间需要另外计算。组件构建报告只衡量本工作区，不代表镜像层大小，也不代表十用户运行内存。设备验收必须绑定实际镜像与组件摘要，不能把旧原型结果算作新镜像的验收。

```bash
python3 -m unittest discover -s tests -v
```

回归覆盖工作区归属、只读 dry-run、容量失败、输入/产物漂移拒绝和 DEX/ZIP 合同。真实组件构建还运行完整 DEX 语义核验与 Android SDK 工具校验。

## 隔离设备验收

设备必须专用于本次测试，使用该派生镜像、新 `/data`、独立 loopback ADB 端口及独立 ADB server；宿主内核需支持 redroid 的 Binder。先确认实际安装的两个 APK 摘要与组件证明一致。验收脚本不会选择或创建设备，也不控制业务容器。

设备启动流程需提供 JSON 文件，含 `owner`（以 `statusbar-image-test-` 开头的唯一归属）、`adb`、`serial`、`adbServerSocket` 和镜像/容器身份。输出目录必须在源码仓库外且不存在。测试会安装十个普通测试 APK、创建独占副屏，并在结束时清理本次应用和屏幕。宿主生命周期测试会 force-stop 状态栏宿主，因此不能指向正在使用的业务实例。

```bash
# 应用夹具的构建与参数见 tests/device-fixture/README.md。
python3 tests/display-probe/build.py --sdk /absolute/sdk --java-home /absolute/jdk17

python3 tests/run_device.py \
  --device /external/evidence/device.json \
  --fixture /external/fixture/fixture-build.json \
  --probe-dex /external/display-probe/dex/classes.dex \
  --providers /absolute/buildploy-device-preview/src/providers.mjs \
  --scrcpy /absolute/scrcpy-server-v3.3.4 \
  --output /external/evidence/new-run --duration 60
```

应用夹具使用[同一源码生成十个独立包](tests/device-fixture/README.md)，在 Android user 0 内按应用 UID 隔离私有数据，不是十个 Android 系统用户。测试验证双屏明暗/真实 Insets/隐藏恢复、非目标屏不受影响、十路共同存活时的实际点击、跨 UID 读私有文件被拒、单应用重启及卸载的数据边界。截图与 H.264 录像保存到证据目录。`result.json` 的 `passed` 表示这些核心断言通过；`lifecycle` 独立记录自动恢复和屏幕销毁清理是否支持，不能把它们的 `false` 当作功能已完成。

视频探针通过显式路径复用 Buildploy 的 `AndroidStream`，全部视频流收到有效首帧后才开始共同窗口。CPU/内存使用该窗口内的 Docker 采样；无编码基线必须排除编码器启动期，阶段名不能替代时间边界。请求的 `fps` 不等同于实际收到的编码帧率，实际值由 `commonWindowStart/End.framePackets` 差值与共同窗口时长计算。VM 内存、宿主 Node/ADB 进程以及浏览器开销需要分开报告；此设备测试不代表十个浏览器的端到端业务验收。
