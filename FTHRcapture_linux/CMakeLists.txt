cmake_minimum_required(VERSION 3.21)
project(FTHRclips_linux CXX C)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# FFmpeg x11grab can block in XCB replies that do not consult the public
# interrupt callback. FTHR therefore never uses it as an in-process UI path:
# the already-existing native engine process owns FFmpeg/XCB, the UI requests
# graceful shutdown, then terminates/kills/reaps that process on fixed bounds.
# No full-resolution frame crosses an additional IPC boundary.
option(FTHR_X11_CAPTURE
    "Enable process-isolated native X11 capture backend" ON)

include(CTest)
if(BUILD_TESTING)
    add_executable(fthr_replay_interval_test
        ../FTHRcapture_common/tests/replay_interval_test.cpp)
    target_include_directories(fthr_replay_interval_test PRIVATE
        ${CMAKE_SOURCE_DIR}/../FTHRcapture_common)
    target_compile_options(fthr_replay_interval_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME replay_interval_contract COMMAND fthr_replay_interval_test)

    add_executable(fthr_raw_fixed_rate_duration_test
        ../FTHRcapture_common/tests/raw_fixed_rate_duration_test.cpp)
    target_include_directories(fthr_raw_fixed_rate_duration_test PRIVATE
        ${CMAKE_SOURCE_DIR}/../FTHRcapture_common)
    target_compile_options(fthr_raw_fixed_rate_duration_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME windows_raw_fixed_rate_duration
        COMMAND fthr_raw_fixed_rate_duration_test)

    add_executable(fthr_windows_encoded_ring_duration_test
        tests/windows_encoded_ring_duration_test.cpp
        ../FTHRcapture/FTHRclips/src/encoded_ring_buffer.cpp
        ../FTHRcapture/FTHRclips/src/encoded_video_config.cpp)
    target_include_directories(fthr_windows_encoded_ring_duration_test PRIVATE
        ../FTHRcapture/FTHRclips/include
        ../FTHRcapture_common)
    target_compile_options(fthr_windows_encoded_ring_duration_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_libraries(fthr_windows_encoded_ring_duration_test PRIVATE pthread)
    add_test(NAME windows_encoded_ring_duration
        COMMAND fthr_windows_encoded_ring_duration_test)

    add_executable(fthr_windows_audio_interval_test
        tests/windows_audio_interval_test.cpp
        ../FTHRcapture/FTHRclips/src/audio_ring_buffer.cpp)
    target_include_directories(fthr_windows_audio_interval_test PRIVATE
        ../FTHRcapture/FTHRclips/include)
    target_compile_options(fthr_windows_audio_interval_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_libraries(fthr_windows_audio_interval_test PRIVATE pthread)
    add_test(NAME windows_audio_interval
        COMMAND fthr_windows_audio_interval_test)

    add_executable(fthr_linux_encoded_ring_duration_test
        tests/linux_encoded_ring_duration_test.cpp
        src/ring_buffer.cpp)
    target_include_directories(fthr_linux_encoded_ring_duration_test PRIVATE
        src
        ../FTHRcapture_common)
    target_compile_options(fthr_linux_encoded_ring_duration_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_libraries(fthr_linux_encoded_ring_duration_test PRIVATE pthread)
    add_test(NAME linux_encoded_ring_duration
        COMMAND fthr_linux_encoded_ring_duration_test)

    add_executable(fthr_capture_recovery_test tests/capture_recovery_test.cpp)
    target_include_directories(fthr_capture_recovery_test PRIVATE src)
    target_compile_options(fthr_capture_recovery_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME capture_recovery_policy COMMAND fthr_capture_recovery_test)

    add_executable(fthr_x11_capture_target_test
        tests/x11_capture_target_test.cpp
        src/x11_capture_target.cpp)
    target_include_directories(fthr_x11_capture_target_test PRIVATE src)
    target_compile_options(fthr_x11_capture_target_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME x11_capture_target COMMAND fthr_x11_capture_target_test)

    add_executable(fthr_transactional_save_test tests/transactional_save_test.cpp)
    target_include_directories(fthr_transactional_save_test PRIVATE
        ${CMAKE_SOURCE_DIR}/../FTHRcapture_common)
    target_compile_options(fthr_transactional_save_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME transactional_save_contract COMMAND fthr_transactional_save_test)
endif()

find_package(PkgConfig REQUIRED)

# Release builds require the pinned FFmpeg headers and libraries from
# tools/fetch_third_party.py --ffmpeg-linux. Set FTHR_FFMPEG_ROOT to that tree;
# headers and runtime libraries must use the same ABI. See BUILDING.md.
set(FTHR_FFMPEG_ROOT "" CACHE PATH
    "Root of the pinned LGPL FFmpeg (contains include/ and lib/). Required for Release builds.")

if(NOT FTHR_FFMPEG_ROOT AND DEFINED ENV{FTHR_FFMPEG_ROOT})
    set(FTHR_FFMPEG_ROOT "$ENV{FTHR_FFMPEG_ROOT}")
endif()

if(FTHR_FFMPEG_ROOT)
    get_filename_component(FTHR_FFMPEG_ROOT "${FTHR_FFMPEG_ROOT}" ABSOLUTE)
    if(NOT EXISTS "${FTHR_FFMPEG_ROOT}/lib/pkgconfig")
        message(FATAL_ERROR
            "FTHR_FFMPEG_ROOT='${FTHR_FFMPEG_ROOT}' has no lib/pkgconfig.\n"
            "Fetch it with:  python tools/fetch_third_party.py --ffmpeg-linux")
    endif()
    # Prepend rather than replace: the pinned tree wins, but pkg-config can
    # still resolve anything it legitimately depends on.
    set(ENV{PKG_CONFIG_PATH} "${FTHR_FFMPEG_ROOT}/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
    set(FTHR_FFMPEG_BUNDLED TRUE)
    message(STATUS "FFmpeg: pinned LGPL distribution at ${FTHR_FFMPEG_ROOT}")
elseif(CMAKE_BUILD_TYPE STREQUAL "Release")
    message(FATAL_ERROR
        "Release build without FTHR_FFMPEG_ROOT.\n"
        "  Refusing to silently link the distribution's FFmpeg, which is a GPL\n"
        "  build and would make the AppImage GPLv3 (AUDIT-014).\n"
        "  Fix:\n"
        "    python tools/fetch_third_party.py --ffmpeg-linux\n"
        "    cmake -S FTHRcapture_linux -B <build> -DCMAKE_BUILD_TYPE=Release \\\n"
        "          -DFTHR_FFMPEG_ROOT=<repo>/FTHRcapture_linux/third_party/ffmpeg\n"
        "  For a development build use -DCMAKE_BUILD_TYPE=Debug (or RelWithDebInfo),\n"
        "  which may use the system FFmpeg but must never be packaged.")
else()
    set(FTHR_FFMPEG_BUNDLED FALSE)
    message(WARNING
        "==================================================================\n"
        " Using the SYSTEM FFmpeg. This is a development build only.\n"
        " On most distributions the system FFmpeg is a GPL build; anything\n"
        " produced from it MUST NOT be packaged or distributed (AUDIT-014).\n"
        "==================================================================")
endif()

pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET
    libavcodec libavfilter libavformat libavutil libavdevice libswscale libswresample)
pkg_check_modules(AVUTIL REQUIRED IMPORTED_TARGET libavutil)

message(STATUS "FFmpeg libavcodec version : ${FFMPEG_libavcodec_VERSION}")
message(STATUS "FFmpeg include dirs       : ${FFMPEG_INCLUDE_DIRS}")
message(STATUS "FFmpeg library dirs       : ${FFMPEG_LIBRARY_DIRS}")
message(STATUS "FFmpeg libraries          : ${FFMPEG_LIBRARIES}")

# A Release build that resolved to /usr anyway means PKG_CONFIG_PATH was
# overridden or the pinned tree is incomplete. Fail here rather than at the
# licence gate twenty minutes later.
if(FTHR_FFMPEG_BUNDLED)
    foreach(_dir ${FFMPEG_LIBRARY_DIRS})
        string(FIND "${_dir}" "${FTHR_FFMPEG_ROOT}" _in_pinned)
        if(_in_pinned EQUAL -1)
            message(FATAL_ERROR
                "FFmpeg resolved to '${_dir}', outside FTHR_FFMPEG_ROOT.\n"
                "The pinned LGPL distribution is not being used.")
        endif()
    endforeach()

    # pkg-config names libraries as -lavcodec/-lswscale/etc.  If a linker alias
    # is absent, the linker silently searches /usr/lib next and can mix the
    # distribution's GPL FFmpeg into this nominally pinned LGPL build.  Resolve
    # every direct engine dependency inside the verified tree and fail closed.
    set(FTHR_FFMPEG_LINK_LIBRARIES)
    foreach(_lib avdevice avfilter avformat avcodec swscale swresample avutil)
        find_library(FTHR_${_lib}_LIBRARY NAMES ${_lib}
            PATHS "${FTHR_FFMPEG_ROOT}/lib" NO_DEFAULT_PATH NO_CACHE)
        if(NOT FTHR_${_lib}_LIBRARY)
            message(FATAL_ERROR
                "Pinned FFmpeg linker alias lib${_lib}.so is missing from "
                "${FTHR_FFMPEG_ROOT}/lib. Re-run: "
                "python tools/fetch_third_party.py --ffmpeg-linux")
        endif()
        list(APPEND FTHR_FFMPEG_LINK_LIBRARIES "${FTHR_${_lib}_LIBRARY}")
    endforeach()
endif()

if(BUILD_TESTING)
    # encoded_video_config.cpp writes metadata through av_dict_set[_int].
    # pthread alone happened to compile this cross-platform contract target,
    # but cannot satisfy those libavutil symbols at link time on Linux.
    target_link_libraries(fthr_windows_encoded_ring_duration_test PRIVATE
        PkgConfig::AVUTIL)
endif()

pkg_check_modules(WAYLAND REQUIRED IMPORTED_TARGET
    wayland-client)
pkg_check_modules(PULSE REQUIRED IMPORTED_TARGET
    libpulse-simple libpulse)

if(BUILD_TESTING)
    add_executable(fthr_wayland_dispatch_test
        tests/wayland_dispatch_test.cpp
        src/wayland_dispatch.cpp)
    target_include_directories(fthr_wayland_dispatch_test PRIVATE src)
    target_link_libraries(fthr_wayland_dispatch_test PRIVATE
        PkgConfig::WAYLAND pthread)
    target_compile_options(fthr_wayland_dispatch_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    add_test(NAME wayland_bounded_dispatch COMMAND fthr_wayland_dispatch_test)
endif()

find_program(WAYLAND_SCANNER wayland-scanner REQUIRED)
set(PROTO_XML_DIR ${CMAKE_CURRENT_SOURCE_DIR}/protocols)
set(PROTO_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/protocols)
file(MAKE_DIRECTORY ${PROTO_GEN_DIR})

# wlr-screencopy
add_custom_command(
    OUTPUT ${PROTO_GEN_DIR}/wlr-screencopy-client-protocol.h
           ${PROTO_GEN_DIR}/wlr-screencopy-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header
            ${PROTO_XML_DIR}/wlr-screencopy-unstable-v1.xml
            ${PROTO_GEN_DIR}/wlr-screencopy-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code
            ${PROTO_XML_DIR}/wlr-screencopy-unstable-v1.xml
            ${PROTO_GEN_DIR}/wlr-screencopy-protocol.c
    DEPENDS ${PROTO_XML_DIR}/wlr-screencopy-unstable-v1.xml
)

# xdg-output
add_custom_command(
    OUTPUT ${PROTO_GEN_DIR}/xdg-output-client-protocol.h
           ${PROTO_GEN_DIR}/xdg-output-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header
            ${PROTO_XML_DIR}/xdg-output-unstable-v1.xml
            ${PROTO_GEN_DIR}/xdg-output-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code
            ${PROTO_XML_DIR}/xdg-output-unstable-v1.xml
            ${PROTO_GEN_DIR}/xdg-output-protocol.c
    DEPENDS ${PROTO_XML_DIR}/xdg-output-unstable-v1.xml
)

# ext-image-copy-capture
add_custom_command(
    OUTPUT ${PROTO_GEN_DIR}/ext-image-copy-capture-client-protocol.h
           ${PROTO_GEN_DIR}/ext-image-copy-capture-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header
            ${PROTO_XML_DIR}/ext-image-copy-capture-v1.xml
            ${PROTO_GEN_DIR}/ext-image-copy-capture-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code
            ${PROTO_XML_DIR}/ext-image-copy-capture-v1.xml
            ${PROTO_GEN_DIR}/ext-image-copy-capture-protocol.c
    DEPENDS ${PROTO_XML_DIR}/ext-image-copy-capture-v1.xml
)

# ext-image-capture-source
add_custom_command(
    OUTPUT ${PROTO_GEN_DIR}/ext-image-capture-source-client-protocol.h
           ${PROTO_GEN_DIR}/ext-image-capture-source-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header
            ${PROTO_XML_DIR}/ext-image-capture-source-v1.xml
            ${PROTO_GEN_DIR}/ext-image-capture-source-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code
            ${PROTO_XML_DIR}/ext-image-capture-source-v1.xml
            ${PROTO_GEN_DIR}/ext-image-capture-source-protocol.c
    DEPENDS ${PROTO_XML_DIR}/ext-image-capture-source-v1.xml
)

# ext-foreign-toplevel-list (required by ext-image-capture-source)
add_custom_command(
    OUTPUT ${PROTO_GEN_DIR}/ext-foreign-toplevel-list-client-protocol.h
           ${PROTO_GEN_DIR}/ext-foreign-toplevel-list-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header
            ${PROTO_XML_DIR}/ext-foreign-toplevel-list-v1.xml
            ${PROTO_GEN_DIR}/ext-foreign-toplevel-list-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code
            ${PROTO_XML_DIR}/ext-foreign-toplevel-list-v1.xml
            ${PROTO_GEN_DIR}/ext-foreign-toplevel-list-protocol.c
    DEPENDS ${PROTO_XML_DIR}/ext-foreign-toplevel-list-v1.xml
)

set(FTHR_ENGINE_SOURCES
    src/main.cpp
    src/shared_memory.cpp
    src/capture_engine.cpp
    src/backend_wlr.cpp
    src/backend_ext.cpp
    src/wayland_dispatch.cpp
    src/capture_backend.cpp
    src/audio_capture.cpp
    src/audio_multi_capture.cpp
    src/encoder.cpp
    src/save_clip.cpp
    src/ring_buffer.cpp
    ${PROTO_GEN_DIR}/wlr-screencopy-protocol.c
    ${PROTO_GEN_DIR}/xdg-output-protocol.c
    ${PROTO_GEN_DIR}/ext-image-copy-capture-protocol.c
    ${PROTO_GEN_DIR}/ext-image-capture-source-protocol.c
    ${PROTO_GEN_DIR}/ext-foreign-toplevel-list-protocol.c
)

if(FTHR_X11_CAPTURE)
    list(APPEND FTHR_ENGINE_SOURCES
        src/backend_x11.cpp
        src/x11_capture_target.cpp
        src/x11_frame_converter.cpp)
endif()

add_executable(FTHRclips ${FTHR_ENGINE_SOURCES})

# The editor loads this small bridge into the Python process.  It uses the same
# pinned LGPL FFmpeg ABI as the capture engine but has no capture/IPC symbols
# and no Qt dependency: FFmpeg decode/mix happens off the GUI thread while
# PySide6 QAudioSink owns final device output.
add_library(FTHRPlaybackMixer SHARED
    ../FTHRcapture_common/ffmpeg_playback_bridge.cpp)

target_include_directories(FTHRclips PRIVATE
    src/
    ${PROTO_GEN_DIR}/
    ${CMAKE_SOURCE_DIR}/../FTHRcapture_common
)
target_include_directories(FTHRclips SYSTEM PRIVATE ${FFMPEG_INCLUDE_DIRS})

target_link_libraries(FTHRclips PRIVATE
    $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
    $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>
    PkgConfig::WAYLAND
    PkgConfig::PULSE
    pthread rt
)

target_compile_options(FTHRclips PRIVATE -Wall -Wextra)
target_compile_definitions(FTHRclips PRIVATE
    FTHR_X11_CAPTURE=$<BOOL:${FTHR_X11_CAPTURE}>)

target_include_directories(FTHRPlaybackMixer PRIVATE
    ${CMAKE_SOURCE_DIR}/../FTHRcapture_common)
target_include_directories(FTHRPlaybackMixer SYSTEM PRIVATE ${FFMPEG_INCLUDE_DIRS})
target_link_libraries(FTHRPlaybackMixer PRIVATE
    $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
    $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>)
target_compile_options(FTHRPlaybackMixer PRIVATE -Wall -Wextra -Wpedantic)

# Use relative $ORIGIN paths for the bundle and development tree. RPATH
# (--disable-new-dtags) also resolves transitive FFmpeg dependencies, unlike
# RUNPATH. BUILD_WITH_INSTALL_RPATH prevents CMake from embedding an absolute
# build-host library path ahead of the bundled runtime.
set_target_properties(FTHRclips PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_RPATH "$ORIGIN:$ORIGIN/lib:$ORIGIN/../lib:$ORIGIN/../third_party/ffmpeg/lib"
    INSTALL_RPATH_USE_LINK_PATH FALSE
    BUILD_RPATH_USE_ORIGIN TRUE)
target_link_options(FTHRclips PRIVATE "-Wl,--disable-new-dtags")
set_target_properties(FTHRPlaybackMixer PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_RPATH "$ORIGIN:$ORIGIN/lib:$ORIGIN/../lib:$ORIGIN/../third_party/ffmpeg/lib"
    INSTALL_RPATH_USE_LINK_PATH FALSE
    BUILD_RPATH_USE_ORIGIN TRUE)
# Shared-library links otherwise allow unresolved symbols, leaving ctypes to
# discover a missing FFmpeg dependency only when the editor opens a clip.
target_link_options(FTHRPlaybackMixer PRIVATE
    "-Wl,--disable-new-dtags" "-Wl,-z,defs")
if(FTHR_FFMPEG_BUNDLED)
    # libavdevice has transitive siblings (notably libavfilter).  Give the
    # static linker the same closed pinned search set used by the runtime RPATH.
    target_link_options(FTHRclips PRIVATE
        "-Wl,-rpath-link,${FTHR_FFMPEG_ROOT}/lib")
    target_link_options(FTHRPlaybackMixer PRIVATE
        "-Wl,-rpath-link,${FTHR_FFMPEG_ROOT}/lib")
endif()

if(BUILD_TESTING)
    add_executable(fthr_playback_bridge_load_test
        tests/playback_bridge_load_test.cpp)
    target_link_libraries(fthr_playback_bridge_load_test PRIVATE ${CMAKE_DL_LIBS})
    add_dependencies(fthr_playback_bridge_load_test FTHRPlaybackMixer)
    add_test(NAME playback_bridge_load
        COMMAND fthr_playback_bridge_load_test $<TARGET_FILE:FTHRPlaybackMixer>)

    if(FTHR_X11_CAPTURE)
        add_executable(fthr_x11_frame_converter_test
            tests/x11_frame_converter_test.cpp
            src/x11_frame_converter.cpp)
        target_include_directories(fthr_x11_frame_converter_test PRIVATE src)
        target_include_directories(fthr_x11_frame_converter_test SYSTEM PRIVATE
            ${FFMPEG_INCLUDE_DIRS})
        target_link_libraries(fthr_x11_frame_converter_test PRIVATE
            $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
            $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>)
        target_compile_options(fthr_x11_frame_converter_test PRIVATE
            -Wall -Wextra -Wpedantic -UNDEBUG)
        target_link_options(fthr_x11_frame_converter_test PRIVATE
            "-Wl,--disable-new-dtags")
        set_target_properties(fthr_x11_frame_converter_test PROPERTIES
            BUILD_WITH_INSTALL_RPATH TRUE
            INSTALL_RPATH "$ORIGIN/../third_party/ffmpeg/lib")
        add_test(NAME x11_frame_conversion
            COMMAND fthr_x11_frame_converter_test)
    endif()

    add_executable(fthr_linux_vaapi_encoder_test
        tests/linux_vaapi_encoder_test.cpp
        src/encoder.cpp)
    target_include_directories(fthr_linux_vaapi_encoder_test PRIVATE src)
    target_include_directories(fthr_linux_vaapi_encoder_test SYSTEM PRIVATE
        ${FFMPEG_INCLUDE_DIRS})
    target_link_libraries(fthr_linux_vaapi_encoder_test PRIVATE
        $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
        $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>
        pthread)
    target_compile_options(fthr_linux_vaapi_encoder_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_options(fthr_linux_vaapi_encoder_test PRIVATE
        "-Wl,--disable-new-dtags")
    set_target_properties(fthr_linux_vaapi_encoder_test PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE
        INSTALL_RPATH "$ORIGIN/../third_party/ffmpeg/lib")
    add_test(NAME linux_vaapi_encoder
        COMMAND fthr_linux_vaapi_encoder_test)

    add_executable(fthr_linux_encoder_timestamp_test
        tests/linux_encoder_timestamp_test.cpp
        src/encoder.cpp)
    target_include_directories(fthr_linux_encoder_timestamp_test PRIVATE src)
    target_include_directories(fthr_linux_encoder_timestamp_test SYSTEM PRIVATE
        ${FFMPEG_INCLUDE_DIRS})
    target_link_libraries(fthr_linux_encoder_timestamp_test PRIVATE
        $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
        $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>
        pthread)
    target_compile_options(fthr_linux_encoder_timestamp_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_options(fthr_linux_encoder_timestamp_test PRIVATE
        "-Wl,--disable-new-dtags")
    set_target_properties(fthr_linux_encoder_timestamp_test PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE
        INSTALL_RPATH "$ORIGIN/../third_party/ffmpeg/lib")
    add_test(NAME linux_encoder_timestamps
        COMMAND fthr_linux_encoder_timestamp_test)

    add_executable(fthr_short_save_mp4_integration_test
        tests/short_save_mp4_integration_test.cpp
        src/ring_buffer.cpp
        src/save_clip.cpp)
    target_include_directories(fthr_short_save_mp4_integration_test PRIVATE
        src
        ../FTHRcapture_common)
    target_include_directories(fthr_short_save_mp4_integration_test SYSTEM PRIVATE
        ${FFMPEG_INCLUDE_DIRS})
    target_link_libraries(fthr_short_save_mp4_integration_test PRIVATE
        $<$<BOOL:${FTHR_FFMPEG_BUNDLED}>:${FTHR_FFMPEG_LINK_LIBRARIES}>
        $<$<NOT:$<BOOL:${FTHR_FFMPEG_BUNDLED}>>:PkgConfig::FFMPEG>
        pthread)
    target_compile_options(fthr_short_save_mp4_integration_test PRIVATE
        -Wall -Wextra -Wpedantic -UNDEBUG)
    target_link_options(fthr_short_save_mp4_integration_test PRIVATE
        "-Wl,--disable-new-dtags")
    set_target_properties(fthr_short_save_mp4_integration_test PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE
        INSTALL_RPATH "$ORIGIN/../third_party/ffmpeg/lib")
    add_test(NAME short_save_actual_mp4
        COMMAND fthr_short_save_mp4_integration_test
            /tmp/fthr-audit042-short-save.mp4)
endif()

install(TARGETS FTHRclips RUNTIME DESTINATION bin)
install(TARGETS FTHRPlaybackMixer LIBRARY DESTINATION lib)
