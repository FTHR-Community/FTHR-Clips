cmake_minimum_required(VERSION 3.18)
project(FTHRclips_linux CXX C)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find required packages
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET
    libavcodec libavformat libavutil libavdevice libswscale libswresample)
pkg_check_modules(WAYLAND REQUIRED IMPORTED_TARGET
    wayland-client)
pkg_check_modules(PULSE REQUIRED IMPORTED_TARGET
    libpulse-simple libpulse)

# Generate wlr-screencopy protocol
find_program(WAYLAND_SCANNER wayland-scanner REQUIRED)
set(WLR_PROTO_DIR ${CMAKE_SOURCE_DIR}/protocols)
set(WLR_PROTO_XML ${WLR_PROTO_DIR}/wlr-screencopy-unstable-v1.xml)

add_custom_command(
    OUTPUT ${WLR_PROTO_DIR}/wlr-screencopy-client-protocol.h
           ${WLR_PROTO_DIR}/wlr-screencopy-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header ${WLR_PROTO_XML}
            ${WLR_PROTO_DIR}/wlr-screencopy-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code ${WLR_PROTO_XML}
            ${WLR_PROTO_DIR}/wlr-screencopy-protocol.c
    DEPENDS ${WLR_PROTO_XML}
)

add_custom_command(
    OUTPUT ${WLR_PROTO_DIR}/xdg-output-client-protocol.h
           ${WLR_PROTO_DIR}/xdg-output-protocol.c
    COMMAND ${WAYLAND_SCANNER} client-header ${WLR_PROTO_DIR}/xdg-output-unstable-v1.xml
            ${WLR_PROTO_DIR}/xdg-output-client-protocol.h
    COMMAND ${WAYLAND_SCANNER} private-code ${WLR_PROTO_DIR}/xdg-output-unstable-v1.xml
            ${WLR_PROTO_DIR}/xdg-output-protocol.c
    DEPENDS ${WLR_PROTO_DIR}/xdg-output-unstable-v1.xml
)

add_executable(FTHRclips
    src/main.cpp
    src/shared_memory.cpp
    src/capture_engine.cpp
    src/audio_capture.cpp
    src/audio_multi_capture.cpp
    src/encoder.cpp
    src/save_clip.cpp
    src/ring_buffer.cpp
    ${WLR_PROTO_DIR}/wlr-screencopy-protocol.c
    ${WLR_PROTO_DIR}/xdg-output-protocol.c
)

target_include_directories(FTHRclips PRIVATE
    src/
    ${WLR_PROTO_DIR}/
)

target_link_libraries(FTHRclips PRIVATE
    PkgConfig::FFMPEG
    PkgConfig::WAYLAND
    PkgConfig::PULSE
    pthread rt
)

target_compile_options(FTHRclips PRIVATE -O2 -Wall -Wextra)

install(TARGETS FTHRclips RUNTIME DESTINATION bin)
