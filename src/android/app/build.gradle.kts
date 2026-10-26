import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 默认不编译 native：只依赖 Android SDK 即可产出可安装 APK（走纯 Kotlin 封装器）。
// 需要 libultrahdr（路线 A 重算 / 官方 API-4 封装）时：
//   ./gradlew assembleDebug -PwithNative=true
val withNative = (project.findProperty("withNative") as String?)?.toBoolean() ?: false

// 发布签名：读 src/android/keystore.properties（该文件与 .jks 均已 gitignore）。
// 缺失时**不创建** release 签名配置，产物保持 unsigned —— 与改动前行为一致，
// 因此 assembleDebug / 现有流程完全不受影响。
// 生成、备份与发版流程见 scripts/release.py 顶部说明。
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}

android {
    namespace = "com.finn.hdr"
    compileSdk = 34

    signingConfigs {
        if (keystorePropsFile.exists()) {
            create("release") {
                // storeFile 建议写绝对路径（keystore 放仓库外，避免误提交）
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
                enableV1Signing = true
                enableV2Signing = true
            }
        }
    }

    defaultConfig {
        applicationId = "com.finn.hdr"
        minSdk = 26          // FileProvider / Bitmap API 足够；HDR"显示"能力由系统看图器决定
        targetSdk = 34
        versionCode = 27
        versionName = "0.1.26"

        if (withNative) {
            ndk {
                abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
            }
            externalNativeBuild {
                cmake {
                    cppFlags += listOf("-std=c++17", "-fexceptions", "-frtti")
                }
            }
        }
    }

    if (withNative) {
        externalNativeBuild {
            cmake {
                path = file("src/main/cpp/CMakeLists.txt")
                version = "3.22.1"
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystorePropsFile.exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.2")
}
