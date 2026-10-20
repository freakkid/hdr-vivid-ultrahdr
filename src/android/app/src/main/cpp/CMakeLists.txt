cmake_minimum_required(VERSION 3.22.1)
project(uhdrjni CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------------------------------------------------------------------------
# libultrahdr 依赖
#
# 本目录：src/android/app/src/main/cpp
# 项目根：../../../../../../  （6 级）
#
# 推荐以 submodule 引入：
#   git submodule add https://github.com/google/libultrahdr third_party/libultrahdr
#   git submodule update --init --recursive
# ---------------------------------------------------------------------------
set(UHDR_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../../../third_party/libultrahdr")

if(NOT EXISTS "${UHDR_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR
        "未找到 libultrahdr：${UHDR_ROOT}\n"
        "请执行：git submodule add https://github.com/google/libultrahdr third_party/libultrahdr\n"
        "     git submodule update --init --recursive\n"
        "（缺失时无法生成 APK —— 本项目的增益图封装依赖它）")
endif()

# 只保留本项目需要的目标，避免拖入 benchmark / 测试
set(UHDR_BUILD_DEPS     ON  CACHE BOOL "" FORCE)   # 自建 libjpeg-turbo / libpng 等依赖
set(UHDR_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(UHDR_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(UHDR_BUILD_BENCHMARK OFF CACHE BOOL "" FORCE)
set(UHDR_BUILD_FUZZERS  OFF CACHE BOOL "" FORCE)
# XMP + MPF 是 Android 14 兼容性最好的组合；如需 ISO 21496-1 再开 UHDR_WRITE_ISO
set(UHDR_WRITE_XMP ON  CACHE BOOL "" FORCE)
set(UHDR_WRITE_ISO OFF CACHE BOOL "" FORCE)

add_subdirectory("${UHDR_ROOT}" "${CMAKE_BINARY_DIR}/libultrahdr")

add_library(uhdrjni SHARED uhdr_jni.cpp)

target_include_directories(uhdrjni PRIVATE "${UHDR_ROOT}")

target_link_libraries(uhdrjni
    ultrahdr
    jnigraphics
    log)
