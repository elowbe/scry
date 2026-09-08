// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Game Stream contributors
//
// Low-latency RGBA8 transport for the MIT-licensed dlss5nr_bridge.dll from
// ComfyUI-DLSS5-Enhancer-Linux. The bridge ABI remains unchanged; conversion
// happens in this native host so Linux/Python never pipes RGB32F frames.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr std::uint32_t kDnr4 = 0x34524E44;  // DNR4: appended FP16 XY motion
constexpr std::uint32_t kDnr3 = 0x33524E44;  // DNR3
constexpr std::uint32_t kFrm3 = 0x334D5246;  // FRM3
constexpr std::uint32_t kOut3 = 0x3354554F;  // OUT3
constexpr std::uint32_t kEnd3 = 0x33444E45;  // END3

using InitFn = int(__cdecl*)(int, const wchar_t*, char*, int);
using ProcessFn = int(__cdecl*)(const float*, const std::uint16_t*, float*, int, int, int, int,
                                int, int, int, float, float, float, float, int, int, char*, int);
using ProcessRgbaFn = int(__cdecl*)(const std::uint8_t*, const std::uint16_t*, std::uint8_t*,
                                    int, int, int, int, int, int, int,
                                    float, float, float, float, int, int, char*, int);
using ShutdownFn = void(__cdecl*)();

struct Header {
    std::uint32_t magic, input_width, input_height, output_width, output_height;
    std::uint32_t warmup_frames, frame_count, perf_quality;
    std::uint32_t profile, preset, style, automask, ui_correction;
    float intensity, tone, structure, skin;
};
struct FrameHeader { std::uint32_t magic, index, reset; };
struct ReplyHeader { std::uint32_t magic, index, ok, byte_count; };
static_assert(sizeof(Header) == 68, "DNR3 header layout changed");
static_assert(sizeof(FrameHeader) == 12, "DNR3 frame header layout changed");
static_assert(sizeof(ReplyHeader) == 16, "DNR3 reply header layout changed");

bool ReadExact(void* destination, std::size_t bytes) {
    auto* dst = static_cast<std::uint8_t*>(destination);
    while (bytes != 0) {
        const std::size_t n = std::fread(dst, 1, bytes, stdin);
        if (n == 0) return false;
        dst += n;
        bytes -= n;
    }
    return true;
}

bool WriteExact(const void* source, std::size_t bytes) {
    return std::fwrite(source, 1, bytes, stdout) == bytes && std::fflush(stdout) == 0;
}

std::wstring AbsolutePath(const wchar_t* path) {
    wchar_t buffer[32768] = {};
    const DWORD n = GetFullPathNameW(path, static_cast<DWORD>(std::size(buffer)), buffer, nullptr);
    return n != 0 && n < std::size(buffer) ? std::wstring(buffer, n) : std::wstring(path);
}

void WriteError(std::uint32_t index, const char* message) {
    ReplyHeader reply{kOut3, index, 0, 0};
    WriteExact(&reply, sizeof(reply));
    const auto length = static_cast<std::uint32_t>(std::min<std::size_t>(std::strlen(message), 65535));
    WriteExact(&length, sizeof(length));
    WriteExact(message, length);
}

template <typename Function>
void ParallelPixels(std::size_t count, Function function) {
    const unsigned available = std::thread::hardware_concurrency();
    const unsigned workers = std::min<unsigned>(8, available ? available : 4);
    if (workers <= 1 || count < 262144) {
        function(0, count);
        return;
    }
    std::vector<std::thread> threads;
    threads.reserve(workers);
    for (unsigned worker = 0; worker < workers; ++worker) {
        const std::size_t begin = count * worker / workers;
        const std::size_t end = count * (worker + 1) / workers;
        threads.emplace_back(function, begin, end);
    }
    for (auto& thread : threads) thread.join();
}

void RgbaToRgbFloat(const std::uint8_t* source, float* target, std::size_t pixels) {
    ParallelPixels(pixels, [=](std::size_t begin, std::size_t end) {
        constexpr float scale = 1.0f / 255.0f;
        for (std::size_t i = begin; i < end; ++i) {
            target[i * 3 + 0] = static_cast<float>(source[i * 4 + 0]) * scale;
            target[i * 3 + 1] = static_cast<float>(source[i * 4 + 1]) * scale;
            target[i * 3 + 2] = static_cast<float>(source[i * 4 + 2]) * scale;
        }
    });
}

void RgbFloatToRgba(const float* source, std::uint8_t* target, std::size_t pixels) {
    ParallelPixels(pixels, [=](std::size_t begin, std::size_t end) {
        for (std::size_t i = begin; i < end; ++i) {
            for (std::size_t channel = 0; channel < 3; ++channel) {
                const float value = std::max(0.0f, std::min(1.0f, source[i * 3 + channel]));
                target[i * 4 + channel] = static_cast<std::uint8_t>(value * 255.0f + 0.5f);
            }
            target[i * 4 + 3] = 255;
        }
    });
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    if (argc != 3) {
        std::fprintf(stderr, "usage: dlss5nr_stream_host.exe RUNTIME_DIRECTORY BRIDGE_DLL\n");
        return 2;
    }
    const std::wstring runtime = AbsolutePath(argv[1]);
    const std::wstring bridge_path = AbsolutePath(argv[2]);
    HMODULE bridge = LoadLibraryW(bridge_path.c_str());
    if (!bridge) {
        std::fprintf(stderr, "LoadLibrary(stream bridge) failed: %lu\n", GetLastError());
        return 3;
    }
    SetDllDirectoryW(runtime.c_str());
    const auto init = reinterpret_cast<InitFn>(GetProcAddress(bridge, "dlss5nr_init"));
    const auto process = reinterpret_cast<ProcessFn>(GetProcAddress(bridge, "dlss5nr_process_v2"));
    const auto process_rgba = reinterpret_cast<ProcessRgbaFn>(
        GetProcAddress(bridge, "dlss5nr_process_rgba8_v3"));
    const auto shutdown = reinterpret_cast<ShutdownFn>(GetProcAddress(bridge, "dlss5nr_shutdown"));
    const auto version = reinterpret_cast<const char*(__cdecl*)()>(GetProcAddress(bridge, "dlss5nr_version"));
    const auto gpu = reinterpret_cast<const char*(__cdecl*)()>(GetProcAddress(bridge, "dlss5nr_gpu_name"));
    if (!init || (!process_rgba && !process) || !shutdown) {
        std::fprintf(stderr, "bridge exports are incomplete\n");
        FreeLibrary(bridge);
        return 4;
    }

    Header header{};
    if (!ReadExact(&header, sizeof(header)) || (header.magic != kDnr3 && header.magic != kDnr4) ||
        header.input_width == 0 || header.input_height == 0 || header.output_width == 0 ||
        header.output_height == 0 || header.input_width > 16384 || header.input_height > 16384 ||
        header.output_width > 16384 || header.output_height > 16384) {
        std::fprintf(stderr, "invalid DNR3 header\n");
        FreeLibrary(bridge);
        return 5;
    }
    const std::uint32_t output_long = std::max(header.output_width, header.output_height);
    const std::uint32_t output_short = std::min(header.output_width, header.output_height);
    if (output_long > 7680 || output_short > 4320) {
        std::fprintf(stderr, "DNR3 output exceeds the 7680x4320 envelope\n");
        FreeLibrary(bridge);
        return 5;
    }
    const std::size_t input_pixels = static_cast<std::size_t>(header.input_width) * header.input_height;
    const std::size_t output_pixels = static_cast<std::size_t>(header.output_width) * header.output_height;
    const bool has_motion = header.magic == kDnr4;
    HANDLE map_file = INVALID_HANDLE_VALUE, mapping = nullptr;
    std::uint8_t* shared = nullptr;
    wchar_t map_path[32768] = {};
    if (GetEnvironmentVariableW(L"GAMESTREAM_FRAME_MAP", map_path, 32768)) {
        map_file = CreateFileW(map_path, GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        if (map_file != INVALID_HANDLE_VALUE)
            mapping = CreateFileMappingW(map_file, nullptr, PAGE_READWRITE, 0, 0, nullptr);
        if (mapping)
            shared = static_cast<std::uint8_t*>(MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0,
                (input_pixels + output_pixels + (has_motion ? input_pixels : 0)) * 4));
        if (!shared) { std::fprintf(stderr, "Frame mapping failed: %lu\n", GetLastError()); return 6; }
    }

    char error[4096] = {};
    int gpu_index = 0;
    char gpu_env[32] = {};
    const DWORD gpu_env_len = GetEnvironmentVariableA("DLSS5NR_GPU_INDEX", gpu_env, sizeof(gpu_env));
    if (gpu_env_len > 0 && gpu_env_len < sizeof(gpu_env)) gpu_index = std::atoi(gpu_env);
    if (!init(std::max(0, gpu_index), runtime.c_str(), error, static_cast<int>(sizeof(error)))) {
        std::fprintf(stderr, "DLSS5 init failed: %s\n", error[0] ? error : "unknown error");
        FreeLibrary(bridge);
        return 7;
    }

    std::vector<std::uint8_t> input_rgba(shared ? 0 : input_pixels * 4), output_rgba(shared ? 0 : output_pixels * 4);
    auto* input_data = shared ? shared : input_rgba.data();
    auto* output_data = shared ? shared + input_pixels * 4 : output_rgba.data();
    std::vector<float> input_rgb, output_rgb;
    if (!process_rgba) {
        input_rgb.resize(input_pixels * 3);
        output_rgb.resize(output_pixels * 3);
    }
    std::vector<std::uint16_t> motion_buffer(input_pixels * 2, 0);
    auto* motion_data = shared && has_motion
        ? reinterpret_cast<std::uint16_t*>(shared + (input_pixels + output_pixels) * 4)
        : motion_buffer.data();
    std::fprintf(stderr, "DLSS5 host ready: %s on %s (%ux%u -> %ux%u, perf=%u, rgba8-stream)\n",
                 version ? version() : "unknown", gpu ? gpu() : "unknown",
                 header.input_width, header.input_height, header.output_width, header.output_height,
                 header.perf_quality);
    std::fprintf(stderr, "DLSS5 transport: %s\n", process_rgba ? "direct-rgba8" : "rgb32f-fallback");

    std::uint32_t expected = 0;
    const bool profile = GetEnvironmentVariableA("GAMESTREAM_DLSS_PROFILE", nullptr, 0) != 0;
    while (header.frame_count == 0 || expected < header.frame_count) {
        const auto frame_started = std::chrono::steady_clock::now();
        FrameHeader frame{};
        const std::size_t first = std::fread(&frame, 1, sizeof(frame), stdin);
        if (first == 0 && std::feof(stdin)) break;
        if (first != sizeof(frame) || frame.magic != kFrm3 || frame.index != expected ||
            (!shared && (!ReadExact(input_data, input_pixels * 4) ||
                         (has_motion && !ReadExact(motion_data, input_pixels * 4))))) {
            std::fprintf(stderr, "invalid stream frame %u\n", expected);
            shutdown();
            FreeLibrary(bridge);
            return 8;
        }
        const auto input_received = std::chrono::steady_clock::now();
        if (!process_rgba) RgbaToRgbFloat(input_data, input_rgb.data(), input_pixels);
        const auto input_converted = std::chrono::steady_clock::now();
        std::memset(error, 0, sizeof(error));
        const int ok = process_rgba
            ? process_rgba(
                input_data, motion_data, output_data,
                static_cast<int>(header.input_width), static_cast<int>(header.input_height),
                static_cast<int>(header.output_width), static_cast<int>(header.output_height),
                static_cast<int>(header.style), static_cast<int>(header.preset),
                static_cast<int>(header.perf_quality), header.intensity, header.tone,
                header.structure, header.skin, static_cast<int>(header.automask),
                frame.reset ? 1 : 0, error, static_cast<int>(sizeof(error)))
            : process(
                input_rgb.data(), motion_data, output_rgb.data(),
                static_cast<int>(header.input_width), static_cast<int>(header.input_height),
                static_cast<int>(header.output_width), static_cast<int>(header.output_height),
                static_cast<int>(header.style), static_cast<int>(header.preset),
                static_cast<int>(header.perf_quality), header.intensity, header.tone,
                header.structure, header.skin, static_cast<int>(header.automask),
                frame.reset ? 1 : 0, error, static_cast<int>(sizeof(error)));
        if (!ok) {
            const char* message = error[0] ? error : "DLSS5 frame failed";
            WriteError(expected, message);
            shutdown();
            FreeLibrary(bridge);
            return 9;
        }
        const auto ngx_complete = std::chrono::steady_clock::now();
        if (!process_rgba) RgbFloatToRgba(output_rgb.data(), output_data, output_pixels);
        const auto output_converted = std::chrono::steady_clock::now();
        const ReplyHeader reply{kOut3, expected, 1, static_cast<std::uint32_t>(output_pixels * 4)};
        if (!WriteExact(&reply, sizeof(reply)) || (!shared && !WriteExact(output_data, output_pixels * 4))) {
            shutdown();
            FreeLibrary(bridge);
            return 10;
        }
        const auto output_written = std::chrono::steady_clock::now();
        if (profile && expected < 16) {
            const auto ms = [](auto begin, auto end) {
                return std::chrono::duration<double, std::milli>(end - begin).count();
            };
            std::fprintf(stderr,
                         "[gamestream-profile] frame=%u input=%.3f convert-in=%.3f ngx=%.3f "
                         "convert-out=%.3f output=%.3f total=%.3f ms\n",
                         expected, ms(frame_started, input_received), ms(input_received, input_converted),
                         ms(input_converted, ngx_complete), ms(ngx_complete, output_converted),
                         ms(output_converted, output_written), ms(frame_started, output_written));
        }
        ++expected;
    }
    const std::uint32_t done = kEnd3;
    WriteExact(&done, sizeof(done));
    shutdown();
    if (shared) UnmapViewOfFile(shared);
    if (mapping) CloseHandle(mapping);
    if (map_file != INVALID_HANDLE_VALUE) CloseHandle(map_file);
    FreeLibrary(bridge);
    return 0;
}
