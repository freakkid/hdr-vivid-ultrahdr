/*
 * libultrahdr JNI 桥接（API-4：SDR 压缩流 + 增益图压缩流 + 元数据 → 直接封装）
 *
 * 为什么用 API-4 而不是 API-0/1：
 *   本场景是「转封装 + 几何变换」，HDR 信息已经在增益图里。走 API-0/1 会让库
 *   重新做 tone mapping 并重算增益图，等于二次损失；API-4 直接把我们已经处理好的
 *   两张 JPEG 按 hdrgm 语义封进容器，画质可控。
 *
 * CMakeLists.txt 关键项：
 *   add_subdirectory(${CMAKE_SOURCE_DIR}/third_party/libultrahdr)
 *   add_library(uhdrjni SHARED uhdr_jni.cpp)
 *   target_link_libraries(uhdrjni ultrahdr jnigraphics log)
 *   # 只写 XMP + MPF（Android 14 兼容优先）；如需 ISO 21496-1 附录 B：
 *   #   set(UHDR_WRITE_ISO ON)  set(UHDR_WRITE_XMP ON)
 */

#include <jni.h>
#include <android/log.h>

#include <cmath>
#include <cstring>
#include <string>
#include <vector>

#include "ultrahdr_api.h"

#define LOG_TAG "UhdrJni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

thread_local std::string g_last_error;

void set_error(const char* stage, const uhdr_error_info_t& err) {
    g_last_error = std::string(stage) + ": code=" + std::to_string(err.error_code) +
                   " has_detail=" + (err.has_detail ? "1" : "0") +
                   (err.has_detail ? (" msg=" + std::string(err.detail)) : "");
    LOGE("%s", g_last_error.c_str());
}

void fill_image(uhdr_compressed_image_t* img, void* data, size_t size) {
    std::memset(img, 0, sizeof(*img));
    img->data = data;
    img->data_sz = size;
    img->capacity = size;
    img->cg = UHDR_CG_UNSPECIFIED;   // 由 JPEG 内嵌 ICC / 默认值决定
    img->ct = UHDR_CT_UNSPECIFIED;
    img->range = UHDR_CR_UNSPECIFIED;
}

bool ok(const uhdr_error_info_t& e) { return e.error_code == UHDR_CODEC_OK; }

}  // namespace

extern "C" {

JNIEXPORT jstring JNICALL
Java_com_finn_hdr_UltraHdrNative_nativeVersion(JNIEnv* env, jobject /*thiz*/) {
    return env->NewStringUTF(UHDR_LIB_VERSION_STR);
}

JNIEXPORT jstring JNICALL
Java_com_finn_hdr_UltraHdrNative_lastError(JNIEnv* env, jobject /*thiz*/) {
    return env->NewStringUTF(g_last_error.c_str());
}

/*
 * 探测容器：只读头部，不解码全图。
 * 返回 [hasGainMap, minBoost, maxBoost, gamma, offsetSdr, offsetHdr, capMin, capMax]
 */
JNIEXPORT jfloatArray JNICALL
Java_com_finn_hdr_UltraHdrNative_nativeProbe(JNIEnv* env, jobject /*thiz*/, jbyteArray jpeg) {
    const jsize len = env->GetArrayLength(jpeg);
    std::vector<uint8_t> buf(static_cast<size_t>(len));
    env->GetByteArrayRegion(jpeg, 0, len, reinterpret_cast<jbyte*>(buf.data()));

    uhdr_codec_private_t* dec = uhdr_create_decoder();
    if (!dec) { g_last_error = "uhdr_create_decoder failed"; return nullptr; }

    uhdr_compressed_image_t img;
    fill_image(&img, buf.data(), buf.size());

    jfloatArray result = nullptr;
    do {
        auto st = uhdr_dec_set_image(dec, &img);
        if (!ok(st)) { set_error("uhdr_dec_set_image", st); break; }

        st = uhdr_dec_probe(dec);
        if (!ok(st)) { set_error("uhdr_dec_probe", st); break; }

        uhdr_gainmap_metadata_t* md = uhdr_dec_get_gainmap_metadata(dec);
        if (!md) {
            result = env->NewFloatArray(1);
            const float none[1] = {0.f};
            env->SetFloatArrayRegion(result, 0, 1, none);
            break;
        }

        const float vals[8] = {
            1.f,
            md->min_content_boost,
            md->max_content_boost,
            md->gamma,
            md->offset_sdr,
            md->offset_hdr,
            md->hdr_capacity_min,
            md->hdr_capacity_max,
        };
        result = env->NewFloatArray(8);
        env->SetFloatArrayRegion(result, 0, 8, vals);
    } while (false);

    uhdr_release_decoder(dec);
    return result;
}

/*
 * API-4 封装：主图与增益图都必须是已按最终尺寸重编码好的 JPEG。
 * 调用方负责保证两者的相对几何关系（增益图通常为主图的 1/2~1/8）。
 */
JNIEXPORT jbyteArray JNICALL
Java_com_finn_hdr_UltraHdrNative_nativeEncode(
    JNIEnv* env, jobject /*thiz*/,
    jbyteArray jsdr, jbyteArray jgainmap,
    jfloat min_boost, jfloat max_boost, jfloat gamma,
    jint base_quality, jint gainmap_quality) {

    const jsize sdr_len = env->GetArrayLength(jsdr);
    const jsize gm_len = env->GetArrayLength(jgainmap);
    std::vector<uint8_t> sdr(static_cast<size_t>(sdr_len));
    std::vector<uint8_t> gm(static_cast<size_t>(gm_len));
    env->GetByteArrayRegion(jsdr, 0, sdr_len, reinterpret_cast<jbyte*>(sdr.data()));
    env->GetByteArrayRegion(jgainmap, 0, gm_len, reinterpret_cast<jbyte*>(gm.data()));

    uhdr_codec_private_t* enc = uhdr_create_encoder();
    if (!enc) { g_last_error = "uhdr_create_encoder failed"; return nullptr; }

    jbyteArray out_arr = nullptr;
    do {
        auto st = uhdr_enc_set_output_format(enc, UHDR_CODEC_JPG);
        if (!ok(st)) { set_error("set_output_format", st); break; }

        uhdr_compressed_image_t base_img;
        uhdr_compressed_image_t gm_img;
        fill_image(&base_img, sdr.data(), sdr.size());
        fill_image(&gm_img, gm.data(), gm.size());

        st = uhdr_enc_set_compressed_image(enc, &base_img, UHDR_BASE_IMG);
        if (!ok(st)) { set_error("set_compressed_image(base)", st); break; }

        uhdr_gainmap_metadata_t md;
        std::memset(&md, 0, sizeof(md));
        md.min_content_boost = min_boost;
        md.max_content_boost = max_boost;
        md.gamma = gamma;
        md.offset_sdr = 1.0f / 64.0f;
        md.offset_hdr = 1.0f / 64.0f;
        md.hdr_capacity_min = min_boost;
        md.hdr_capacity_max = max_boost;
        md.use_base_cg = 1;

        st = uhdr_enc_set_gainmap_image(enc, &gm_img, &md);
        if (!ok(st)) { set_error("set_gainmap_image", st); break; }

        uhdr_enc_set_quality(enc, base_quality, UHDR_BASE_IMG);
        uhdr_enc_set_quality(enc, gainmap_quality, UHDR_GAIN_MAP_IMG);

        st = uhdr_encode(enc);
        if (!ok(st)) { set_error("uhdr_encode", st); break; }

        uhdr_compressed_image_t* encoded = uhdr_get_encoded_stream(enc);
        if (!encoded || !encoded->data) { g_last_error = "empty encoded stream"; break; }

        out_arr = env->NewByteArray(static_cast<jsize>(encoded->data_sz));
        env->SetByteArrayRegion(out_arr, 0, static_cast<jsize>(encoded->data_sz),
                                reinterpret_cast<const jbyte*>(encoded->data));
    } while (false);

    uhdr_release_encoder(enc);
    return out_arr;
}

}  // extern "C"
