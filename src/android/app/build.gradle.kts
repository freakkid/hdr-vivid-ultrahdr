plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 默认不编译 native：只依赖 Android SDK 即可产出可安装 APK（走纯 Kotlin 封装器）。
// 需要 libultrahdr（路线 A 重算 / 官方 API-4 封装）时：
//   ./gradlew assembleDebug -PwithNative=true
val withNative = (project.findProperty("withNative") as String?)?.toBoolean() ?: false

android {
    namespace = "com.finn.hdr"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.finn.hdr"
        minSdk = 26          // FileProvider / Bitmap API 足够；HDR"显示"能力由系统看图器决定
        targetSdk = 34
        versionCode = 20
        versionName = "0.1.19"

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
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.2")
}
