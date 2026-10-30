import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const repository = resolve(root, '../..');
const output = resolve(process.argv[2] || join(homedir(), 'dev/build/redroid-android15/artifacts/device-fixture'));
if (output === repository || output.startsWith(repository + sep)) {
  throw new Error('Fixture outputs and debug key must be outside the source repository');
}
const sdk = process.env.ANDROID_HOME || process.env.ANDROID_SDK_ROOT || '/opt/buildploy/android-sdk';
const tools = join(sdk, 'build-tools', '35.0.0');
const android = join(sdk, 'platforms', 'android-35', 'android.jar');
const java = name => process.env.JAVA_HOME ? join(process.env.JAVA_HOME, 'bin', name) : name;
const run = (command, args, cwd = root) => execFileSync(command, args, { cwd, stdio: 'inherit' });
const capture = (command, args) => execFileSync(command, args, { encoding: 'utf8' });
const sha = path => createHash('sha256').update(readFileSync(path)).digest('hex');
const compiler = capture(java('javac'), ['-version']).trim();
if (!/^javac 17\./.test(compiler)) throw new Error(`JDK 17 required, got ${compiler}`);
mkdirSync(output, { recursive: true });
const staging = mkdtempSync(join(output, '.fixture-build-'));
const key = join(output, 'fixture.jks');
try {
  const classes = join(staging, 'classes');
  const dex = join(staging, 'dex');
  mkdirSync(classes); mkdirSync(dex);
  const source = join(root, 'src/probe/StatusBarProbe.java');
  run(java('javac'), ['--release', '11', '-classpath', android, '-d', classes, source]);
  run(java('jar'), ['cf', join(staging, 'classes.jar'), '-C', classes, '.']);
  run(join(tools, 'd8'), ['--min-api', '30', '--lib', android, '--output', dex, join(staging, 'classes.jar')]);
  if (!existsSync(key)) {
    run(java('keytool'), ['-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android',
      '-alias', 'statusbar-probe', '-keyalg', 'RSA', '-dname', 'CN=Buildploy Isolated Status Bar Probe', '-validity', '3650']);
  }
  chmodSync(key, 0o600);
  const variants = Array.from({ length: 10 }, (_, index) => ({
    name: `session${index + 1}`, light: index % 2 === 0,
  }));
  const packages = [];
  for (const variant of variants) {
    const directory = join(staging, variant.name); mkdirSync(directory);
    const packageName = `com.buildploy.statusbarprobe.${variant.name}`;
    const manifest = join(directory, 'AndroidManifest.xml');
    writeFileSync(manifest, `<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="${packageName}" android:versionCode="2" android:versionName="2.0">
  <uses-sdk android:minSdkVersion="30" android:targetSdkVersion="35" />
  <application android:label="Status Bar Probe ${variant.name}" android:theme="@style/ProbeTheme" android:allowBackup="false" android:supportsRtl="true">
    <meta-data android:name="probeLightTheme" android:value="${variant.light}" />
    <activity android:name="probe.StatusBarProbe" android:exported="true" android:launchMode="singleTop" android:resizeableActivity="true">
      <intent-filter><action android:name="android.intent.action.MAIN" /><category android:name="android.intent.category.LAUNCHER" /></intent-filter>
    </activity>
  </application>
</manifest>
`);
    const unsigned = join(directory, 'unsigned.apk');
    const apk = join(output, `statusbar-probe-${variant.name}.apk`);
    run(join(tools, 'aapt'), ['package', '-f', '-M', manifest, '-S', join(root, 'res'), '-I', android, '-F', unsigned]);
    run(join(tools, 'aapt'), ['add', unsigned, 'classes.dex'], dex);
    run(join(tools, 'zipalign'), ['-f', '4', unsigned, apk]);
    run(join(tools, 'apksigner'), ['sign', '--ks', key, '--ks-pass', 'pass:android', '--v4-signing-enabled', 'false', apk]);
    const signature = capture(join(tools, 'apksigner'), ['verify', '--verbose', '--print-certs', apk]);
    writeFileSync(join(output, `${variant.name}-signature.txt`), signature);
    const badging = capture(join(tools, 'aapt'), ['dump', 'badging', apk]);
    if (!badging.includes(`package: name='${packageName}'`) || !badging.includes("targetSdkVersion:'35'")) {
      throw new Error(`Unexpected fixture manifest: ${variant.name}`);
    }
    writeFileSync(join(output, `${variant.name}-badging.txt`), badging);
    packages.push({ packageName, component: `${packageName}/probe.StatusBarProbe`, apk,
      sha256: sha(apk), defaultDarkIcons: variant.light, lightBackground: variant.light,
      compileSdk: 35, targetSdk: 35, minSdk: 30 });
  }
  const metadata = {
    builtAt: new Date().toISOString(), compiler, sdk, buildTools: '35.0.0',
    sourceHashes: Object.fromEntries(['src/probe/StatusBarProbe.java', 'res/values/styles.xml', 'build.mjs']
      .map(name => [name, sha(join(root, name))])),
    dexSha256: sha(join(dex, 'classes.dex')), packages, androidUser: 0,
    isolation: 'Separate package/UID private SharedPreferences; no sharedUserId',
    installed: false,
  };
  writeFileSync(join(output, 'fixture-build.json'), JSON.stringify(metadata, null, 2) + '\n');
  process.stdout.write(JSON.stringify(metadata, null, 2) + '\n');
} finally {
  rmSync(staging, { recursive: true, force: true });
}
