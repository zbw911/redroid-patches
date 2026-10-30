# redroid patches 与共享副屏状态栏组件

本 Fork 在上游 redroid 补丁之外，提供固定 Android 15 镜像的 **SystemUI 组件补丁＋状态栏宿主 APK** 构建入口。它直接校验并修改固定版本的 SystemUI APK，再生成派生镜像的构建上下文，不需要同步或编译整个 AOSP。

当前阶段是可复现的组件构建准备：迁移已验证原型、固定输入和工具链、生成并校验两个 APK、准备镜像入口。宿主仍是原型：依赖 Activity 存活，时钟和图标为静态素材，仅支持统一的图标明暗；尚未实现进程恢复、显示屏移除/重建、状态重放与旋转适配，也没有通知下拉或导航栏。镜像启动后不会自动启动宿主或绑定副屏。完整生命周期及新镜像的双屏、十用户验收仍需后续完成。

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

组件输出的 `build.json` 记录输入锁、源码摘要、工具版本、APK 验证、构建耗时和磁盘增量；`image/` 仅含两个 APK、Dockerfile 与 dockerignore。源码、锁或上下文变化后，旧构建不能用于 `image`，需重新构建。

## 空间与验证范围

此路线不再需要 AOSP 的数百 GB 工作区。组件操作预留 2 GiB 可用空间；这是构建余量门槛，不是实测占用。`doctor` 同时报告 WSL 文件系统容量和对应 Windows 宿主盘实际空闲容量；Windows 盘查询失败时状态为未知，实际构建会停止，不能拿 VHD 内的虚拟容量代替。

首次拉取基础镜像、Docker 层和 SDK/JDK 安装空间需要另外计算。组件构建报告只衡量本工作区，不代表镜像层大小，也不代表十用户运行内存。设备验收必须绑定实际镜像与组件摘要，不能把旧原型结果算作新镜像的验收。

```bash
python3 -m unittest discover -s tests -v
```

回归覆盖工作区归属、只读 dry-run、容量失败、输入/产物漂移拒绝和 DEX/ZIP 合同。真实组件构建还运行完整 DEX 语义核验与 Android SDK 工具校验；镜像启动、生命周期和多用户行为需要在后续隔离设备上验证。
