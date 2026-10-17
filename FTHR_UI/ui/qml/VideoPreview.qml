import QtQuick
import QtMultimedia

Rectangle {
    id: root
    color: backgroundColor
    property color backgroundColor: "#000000"
    property color accentColor: "#00e0c0"
    property real sourceAspect: 1.7777778
    property real stretchRatio: 1
    property real exposure: 0
    property real contrast: 1
    property real saturation: 1
    property real temperature: 0
    property real sharpness: 0
    property real vignette: 0
    property bool effectsActive: false
    property bool cropActive: false
    property vector4d cropArea: Qt.vector4d(0, 0, 1, 1)
    property string cropLabel: ""
    property alias videoSink: video.videoSink
    signal shaderFailed()

    Item {
        id: display
        property real aspect: root.sourceAspect * root.stretchRatio
        width: Math.min(root.width, root.height * aspect)
        height: width / aspect
        anchors.centerIn: parent

        VideoOutput {
            id: video
            anchors.fill: parent
            fillMode: VideoOutput.Stretch
            endOfStreamPolicy: VideoOutput.KeepLastFrame
            layer.enabled: root.effectsActive
            layer.smooth: true
            layer.effect: ShaderEffect {
                property real exposure: root.exposure
                property real contrast: root.contrast
                property real saturation: root.saturation
                property real temperature: root.temperature
                property real sharpness: root.sharpness
                property real vignette: root.vignette
                property vector2d pixelStep: Qt.vector2d(1 / Math.max(1, video.width),
                                                       1 / Math.max(1, video.height))
                property vector4d cropArea: root.cropArea
                property color backgroundColor: root.backgroundColor
                fragmentShader: "color.frag.qsb"
                onStatusChanged: if (status === ShaderEffect.Error) root.shaderFailed()
            }
        }
        Item {
            id: crop
            visible: root.cropActive
            x: root.cropArea.x * display.width
            y: root.cropArea.y * display.height
            width: root.cropArea.z * display.width
            height: root.cropArea.w * display.height
            Rectangle { x: -crop.x; y: -crop.y; width: display.width; height: crop.y; color: root.backgroundColor; opacity: 0.32 }
            Rectangle { x: -crop.x; y: crop.height; width: display.width; height: display.height - crop.y - crop.height; color: root.backgroundColor; opacity: 0.32 }
            Rectangle { x: -crop.x; width: crop.x; height: crop.height; color: root.backgroundColor; opacity: 0.32 }
            Rectangle { x: crop.width; width: display.width - crop.x - crop.width; height: crop.height; color: root.backgroundColor; opacity: 0.32 }
            Rectangle { anchors.fill: parent; color: "transparent"; border.color: root.accentColor; border.width: 3 }
            Rectangle {
                x: Math.max(6 - crop.x, 0)
                y: Math.max(6 - crop.y, -28)
                width: label.implicitWidth + 16
                height: 22
                color: root.backgroundColor
                border.color: root.accentColor
                Text { id: label; anchors.centerIn: parent; text: root.cropLabel; color: root.accentColor; font.pixelSize: 12; font.bold: true }
            }
        }
    }
}
