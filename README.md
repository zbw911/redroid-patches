# redroid patches 与共享副屏状态栏组件

本 Fork 在上游 redroid 补丁之外，提供固定 Android 15 镜像的 **SystemUI 组件补丁＋状态栏宿主 APK** 构建入口。它直接校验并修改固定版本的 SystemUI APK，再生成派生镜像的构建上下文，不需要同步或编译整个 AOSP。

入口包含固定输入和工具链、APK 校验、派生镜像构建，以及隔离设备上的双屏与十会话验收。状态栏由持久化系统 APK 宿主管理，按预览身份绑定副屏，并从真实 SystemUI 状态恢复图标明暗。当前仍使用静态时钟、电量和信号素材，只支持统一的图标明暗，不提供通知下拉或导航栏；实现与当前镜像的实际设备验收需分别核对。

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

SystemUI 输入必须同时匹配原 APK 摘要和平台证书。补丁追加 bridge DEX，只允许三处语义变化：

- 在 `CommandQueue.onSystemBarAttributesChanged` 入口增加一次观察调用，原回调继续执行。
- 在 `CentralSurfacesImpl.start()` 的原注册位置，将单次注册改为由 bridge 调用 `registerStatusBarForAllDisplays`，仍传入原来的同一个 `CommandQueue`，缓存所有屏幕的真实状态，再把默认屏结果交回原启动流程。不会另注册全局 endpoint，也不会在每次查询时重复注册。
- 在 `ShellTaskOrganizer.registerOrganizer()` 重放已有任务列表时包装原 `onTaskAppeared` 调用：先执行原回调，再为当前仍可见、位于 root/system 所有的可信 `buildploy-preview-*` 副屏上的有效任务 leash 恢复显示。原回调异常保持原样，补充恢复失败只记录日志；不修改新任务回调入口或主屏任务。

固定基础镜像必须具有该全屏注册接口。隐藏返回类型的占位类只用于编译，不打入 APK，运行时使用基础镜像的真实 framework 类。验证覆盖：

- DEX039 的 magic、112 字节 header、file_size，以及 Android `dexdump` 解析。
- 被修改 DEX 的全部类、字段、方法、指令与 try/catch 语义；只允许上述回调前缀与两处原位调用替换，替换调用的寄存器、指令宽度及原控制流不变。调试信息不在语义检查范围内，其余原 DEX 按字节核验。
- 原 APK 的非目标 ZIP 内容（含 manifest/resources）保持字节一致，新增内容只允许 bridge DEX 与签名。
- 两个输出 APK 的签名证书、16 KiB 页对齐；宿主包名及 SDK 级别。

签名材料从外部路径传入，不复制到构建上下文，也不进入 Git。固定基础镜像使用 AOSP 的公开开发测试平台证书；需要与它匹配的开发测试密钥，任意新建 debug keystore 均不适用。更换基础镜像或签名不能只改一个 tag，必须重新提取、锁定并验证完整输入合同。

## 宿主与预览生命周期

宿主使用 `persistent`、`directBootAware` 的 `Application`，不再依赖 Activity 或 `.Commands` 广播。控制入口是设备内的 `buildploy-statusbar-host-v1` LocalSocket，仅接受对端 UID 0；`hello/status/bind/release` 不接收手工颜色。平台 helper 负责创建、释放副屏，宿主只拥有该屏的透明 `TYPE_STATUS_BAR` 窗口及真实 Insets，不创建或接管副屏。

绑定必须精确匹配 `runtimeId/generation/packageName/displayId/displayUniqueId`；`generation` 为字符串。宿主还核对预览屏名称、root/system 所有者和应用 UID。仅合法绑定身份通过 `AtomicFile` 保存在设备保护存储中，同时记录 boot count、屏名称、应用及屏所有者 UID，不持久化图标颜色。宿主进程恢复后只重建仍匹配这些条件的窗口，并用新 nonce 请求重放；不会扫描并认领其他屏幕。设备重启后，旧 boot count 的绑定失效，副屏由 helper 按新的预览生命周期建立和绑定。

宿主与 bridge 通过签名权限保护的定向广播通信，双方校验共享的真实发送包/UID 及平台签名。bridge 在内存中保留真实回调和启动快照，重放及后续事件均携带完整绑定身份、请求 nonce、bridge 进程代次和事件序号；旧身份、旧 nonce、旧序号不能更新当前窗口。宿主收到状态前保持透明、不绘制图标；无匹配应用状态时返回 pending，不推测明暗。SystemUI 重启后，原单次注册重新取得 system_server 保存的全屏快照，再通过 `bridge-ready` 通知宿主重新绑定，无需应用再次切换样式。

SystemUI 同时承载 Shell 任务组织器，进程死亡还会触发原有任务 Surface 的迁移。状态事件重放或宿主绘制日志不能单独证明应用画面已恢复；已有任务恢复钩子处理该显示边界，设备断言还必须核对原副屏的实际画面和触摸，不能用重建应用或副屏替代恢复。

屏幕移除、身份失效或绑定应用卸载时清理对应窗口和记录；屏幕变化会重新核对身份并更新窗口尺寸。混合明暗的 `AppearanceRegion` 当前返回不支持并保留此前有效样式，首次绑定则保持未绘制；旋转、进程恢复和销毁边界应以当前镜像的对应设备断言为准，不能由编译成功推断通过。

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

设备启动流程需提供 JSON 文件，含 `owner`（以 `statusbar-image-test-` 开头的唯一归属）、`adb`、`serial`、`adbServerSocket` 和镜像/容器身份。输出目录必须在源码仓库外且不存在。测试通过真实平台 helper 创建、绑定和释放独占副屏，安装十个普通测试 APK，并在结束时清理本次资源。生命周期验收会精确终止该设备的宿主或 SystemUI 进程，因此不能指向正在使用的业务实例；`force-stop` 后手工启动不能证明自动恢复。

```bash
# 应用夹具的构建与参数见 tests/device-fixture/README.md。
python3 tests/display-probe/build.py --sdk /absolute/sdk --java-home /absolute/jdk17

python3 tests/run_device.py \
  --device /external/evidence/device.json \
  --fixture /external/fixture/fixture-build.json \
  --helper-apk /external/buildploy-helper/helper.apk \
  --probe-dex /external/display-probe/dex/classes.dex \
  --providers /absolute/buildploy-device-preview/src/providers.mjs \
  --scrcpy /absolute/scrcpy-server-v3.3.4 \
  --output /external/evidence/new-run --duration 60
```

`--helper-apk` 必须来自对应平台 helper 源码；测试探针只负责 root socket 调用、快照和边界检查，不替代平台的副屏生命周期实现。应用夹具使用[同一源码生成十个独立包](tests/device-fixture/README.md)，在 Android user 0 内按应用 UID 隔离私有数据，不是十个 Android 系统用户。

验收覆盖双屏明暗/真实 Insets/隐藏恢复、非目标屏不受影响、十路共同存活时的实际点击、跨 UID 读私有文件被拒、单应用重启及卸载的数据边界。生命周期断言还需验证带绑定的宿主意外死亡后自动恢复窗口和当前明暗、SystemUI 重启后快照重放、屏幕移除与失效身份清理；失败不能作为独立的 `lifecycle=false` 从总结果中排除。截图、H.264 录像及结构化断言保存到证据目录，只有当前镜像的实际结果才能作为验收结论；空载宿主自动启动或编译通过不代表这些场景已通过。

视频探针通过显式路径复用 Buildploy 的 `AndroidStream`，全部视频流收到有效首帧后才开始共同窗口。CPU/内存使用该窗口内的 Docker 采样；无编码基线必须排除编码器启动期，阶段名不能替代时间边界。请求的 `fps` 不等同于实际收到的编码帧率，实际值由 `commonWindowStart/End.framePackets` 差值与共同窗口时长计算。VM 内存、宿主 Node/ADB 进程以及浏览器开销需要分开报告；此设备测试不代表十个浏览器的端到端业务验收。
