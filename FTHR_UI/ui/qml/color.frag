#version 440
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float exposure;
    float contrast;
    float saturation;
    float temperature;
    float sharpness;
    float vignette;
    vec2 pixelStep;
    vec4 cropArea;
    vec4 backgroundColor;
};
layout(binding = 1) uniform sampler2D source;

vec3 grade(vec2 uv) {
    vec3 rgb = texture(source, clamp(uv, pixelStep * 0.5, vec2(1.0) - pixelStep * 0.5)).rgb;
    rgb = (rgb - 0.5) * contrast + 0.5 + exposure;
    rgb = mix(vec3(dot(rgb, vec3(0.2126, 0.7152, 0.0722))), rgb, saturation);
    rgb *= vec3(1.0 + temperature, 1.0, 1.0 - temperature);
    return floor(clamp(rgb, 0.0, 1.0) * 255.0 + 0.5) / 255.0;
}

void main() {
    vec2 uv = qt_TexCoord0;
    vec3 rgb = grade(uv);
    if (sharpness > 0.0) {
        float weights[5] = float[](1.0, 4.0, 6.0, 4.0, 1.0);
        vec3 blurred = vec3(0.0);
        for (int y = -2; y <= 2; ++y)
            for (int x = -2; x <= 2; ++x)
                blurred += grade(uv + vec2(x, y) * pixelStep) * weights[x + 2] * weights[y + 2] / 256.0;
        rgb = clamp(rgb * (1.0 + sharpness) - blurred * sharpness, 0.0, 1.0);
    }
    vec2 cropUV = (uv - cropArea.xy) / max(cropArea.zw, vec2(0.0001));
    if (vignette > 0.0 && all(greaterThanEqual(cropUV, vec2(0.0))) && all(lessThanEqual(cropUV, vec2(1.0)))) {
        vec2 cropSize = cropArea.zw / pixelStep;
        float radius = max(cropSize.x, cropSize.y) * 0.72;
        float distanceFromCenter = length((cropUV - 0.5) * cropSize) / radius;
        rgb = mix(rgb, backgroundColor.rgb, clamp((distanceFromCenter - 0.35) / 0.65, 0.0, 1.0) * vignette);
    }
    fragColor = vec4(rgb, 1.0) * qt_Opacity;
}
