import QtQuick

Item {
    id: root
    property url outgoing
    property url incoming
    property bool flight: false
    property bool horizontal: true
    property real progress: 0
    property int direction: 1
    property real frameX: 0
    property real frameY: 0
    property real frameWidth: width
    property real frameHeight: height
    property real frameOpacity: 1
    readonly property real distance: horizontal ? width : height
    readonly property real offset: Math.round(distance * progress) * direction
    clip: true

    Image {
        source: root.outgoing
        cache: false
        asynchronous: false
        smooth: root.flight
        x: root.flight ? root.frameX : (root.horizontal ? -root.offset : 0)
        y: root.flight ? root.frameY : (root.horizontal ? 0 : -root.offset)
        width: root.flight ? root.frameWidth : root.width
        height: root.flight ? root.frameHeight : root.height
        opacity: root.flight ? root.frameOpacity : 1
    }
    Image {
        visible: !root.flight
        source: root.incoming
        cache: false
        asynchronous: false
        smooth: false
        x: root.horizontal ? root.distance * root.direction - root.offset : 0
        y: root.horizontal ? 0 : root.distance * root.direction - root.offset
        width: root.width
        height: root.height
    }
}
