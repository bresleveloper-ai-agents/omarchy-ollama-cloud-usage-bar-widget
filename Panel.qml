import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Ollama Cloud usage: one bar button and one popup panel. collect.py does all
// the talking to ollama.com and writes a record; this file only runs it on a
// timer and draws whatever the record says. Layout and components mirror the
// stock omarchy.agents panel so the two read as siblings.
Panel {
  id: root
  moduleName: "ariel.ollama-usage"
  ipcTarget: "ariel.ollama-usage"
  // Our IpcHandler below adds `refresh`; the base one would share the target.
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Color.popups.background
  readonly property color track: Style.selectedFillFor(foreground, Color.accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Manifest defaults are not merged into the bar entry, so every read
  // carries its own fallback.
  readonly property int refreshIntervalSec: Math.max(60, Number(setting("refreshIntervalSec", 900)) || 900)
  readonly property string barIcon: String(setting("icon", "󱚤")) || "󱚤"
  readonly property bool showPercent: String(setting("showPercent", "Off")).toLowerCase() === "on"

  readonly property string home: Quickshell.env("HOME")
  readonly property string stateFile: (Quickshell.env("XDG_STATE_HOME") || home + "/.local/state") + "/omarchy/ollama-usage/usage.json"
  readonly property string collector: decodeURIComponent(Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, ""))

  property var record: null
  property double nowMs: Date.now()

  readonly property var windows: record && Array.isArray(record.windows) ? record.windows : []
  readonly property var headline: {
    var best = null
    for (var i = 0; i < windows.length; i++)
      if (!best || Number(windows[i].percent) > Number(best.percent)) best = windows[i]
    return best
  }
  readonly property bool alarming: !!headline && Number(headline.percent) >= 0.9
  readonly property bool hasProblem: !!record && (String(record.authHelp || "") !== "" || String(record.error || "") !== "")

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }
  function percentText(w) { return w ? Math.round(Number(w.percent || 0) * 100) + "%" : "—" }

  function ageText(iso) {
    var ms = new Date(String(iso || "")).getTime()
    if (!isFinite(ms)) return ""
    var minutes = Math.floor(Math.max(0, root.nowMs - ms) / 60000)
    if (minutes < 1) return "just now"
    if (minutes < 60) return minutes + "m ago"
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return hours + "h " + (minutes % 60) + "m ago"
    return Math.floor(hours / 24) + "d ago"
  }

  function heroMeta() {
    if (!record) return collectProcess.running ? "Checking…" : "No data yet"
    if (String(record.authHelp || "") !== "" && windows.length === 0) return "Not connected"
    var age = ageText(record.updatedAt)
    if (record.stale) return "Stale · updated " + age
    return age !== "" ? "Updated " + age : ""
  }

  function barTooltip() {
    if (windows.length === 0) return "Ollama Cloud" + (hasProblem ? " · needs attention" : "")
    var parts = []
    for (var i = 0; i < windows.length; i++) parts.push(windows[i].title + " " + percentText(windows[i]))
    var age = record ? ageText(record.updatedAt) : ""
    return "Ollama Cloud · " + parts.join(" · ") + (age !== "" ? " · " + age : "")
  }

  // Model rows scale to the busiest model; tools (web search) are listed
  // after the models and never set the scale.
  function modelPeak(window) {
    var peak = 1
    var models = window && window.models ? window.models : []
    for (var i = 0; i < models.length; i++)
      if (!models[i].tool) peak = Math.max(peak, Number(models[i].requests || 0))
    return peak
  }

  function orderedRows(window) {
    var models = window && window.models ? window.models : []
    return models.filter(function(m) { return !m.tool }).concat(models.filter(function(m) { return !!m.tool }))
  }

  function iconCandidates() {
    var light = colorLuminance(root.surface) >= 0.5
    var out = []
    if (light) out.push(Qt.resolvedUrl("assets/ollama-light.svg"))
    out.push(Qt.resolvedUrl("assets/ollama.svg"))
    return out
  }

  function channelLuminance(v) {
    var c = Number(v)
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
  }

  function colorLuminance(c) {
    return 0.2126 * channelLuminance(c.r) + 0.7152 * channelLuminance(c.g) + 0.0722 * channelLuminance(c.b)
  }

  // ------------------------------------------------------------ data flow

  function refresh(force) {
    if (collectProcess.running) return
    collectProcess.command = force
      ? ["timeout", "-k", "5", "30", "python3", root.collector, "--force"]
      : ["timeout", "-k", "5", "30", "python3", root.collector]
    collectProcess.running = true
  }

  Process {
    id: collectProcess
    running: false
    onExited: stateView.reload()
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("ollama-usage", text.trim())
    }
  }

  FileView {
    id: stateView
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var parsed = JSON.parse(String(text() || ""))
        root.record = parsed && typeof parsed === "object" ? parsed : null
      } catch (e) {
        console.warn("ollama-usage", "ignoring unreadable state file", e)
      }
    }
  }

  // A short delay lets the session settle (network, keyring) after login.
  Timer {
    interval: 5000
    running: true
    repeat: false
    onTriggered: root.refresh(false)
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    onTriggered: root.refresh(false)
  }

  // After a transient failure, try once more soon instead of waiting out the
  // full interval.
  Timer {
    id: retryTimer
    interval: 60000
    running: !!root.record && root.record.stale === true
    repeat: false
    onTriggered: root.refresh(true)
  }

  Timer {
    interval: 30000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  onOpenedChanged: if (opened) {
    nowMs = Date.now()
    if (panelFlick) panelFlick.contentY = 0
    // collect.py itself skips the call when the last check is under a minute old.
    refresh(false)
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(true); return "ok" }
  }

  // --------------------------------------------------------------- bar

  implicitWidth: showPercent ? percentButton.implicitWidth : iconButton.implicitWidth
  implicitHeight: showPercent ? percentButton.implicitHeight : iconButton.implicitHeight

  function handleBarPress(buttonCode) {
    if (buttonCode === Qt.MiddleButton) root.refresh(true)
    else if (buttonCode === Qt.RightButton) {
      if (root.bar) root.bar.run("xdg-open https://ollama.com/settings")
    }
    else root.toggle()
  }

  BarIconButton {
    id: iconButton
    visible: !root.showPercent
    anchors.fill: parent
    bar: root.bar
    text: root.barIcon
    active: root.alarming
    tooltipText: root.barTooltip()
    onPressed: function(buttonCode) { root.handleBarPress(buttonCode) }
  }

  WidgetButton {
    id: percentButton
    visible: root.showPercent
    anchors.fill: parent
    bar: root.bar
    text: root.barIcon + (root.headline ? " " + root.percentText(root.headline) : "")
    active: root.alarming
    tooltipText: root.barTooltip()
    onPressed: function(buttonCode) { root.handleBarPress(buttonCode) }
  }

  // ------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: root.showPercent ? percentButton : iconButton
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.refresh(true)
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refresh(true) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------- Hero ----------
          PanelHero {
            width: parent.width
            title: "Ollama Cloud"
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Item {
                id: heroMark
                property var candidates: root.iconCandidates()
                property string candidatesKey: candidates.join("\n")
                property int candidateIndex: 0
                onCandidatesKeyChanged: candidateIndex = 0

                width: Style.font.display
                height: Style.font.display

                Image {
                  id: heroImage
                  anchors.fill: parent
                  source: heroMark.candidateIndex < heroMark.candidates.length ? heroMark.candidates[heroMark.candidateIndex] : ""
                  sourceSize.width: Style.font.display * 2
                  sourceSize.height: Style.font.display * 2
                  fillMode: Image.PreserveAspectFit
                  onStatusChanged: if (status === Image.Error && heroMark.candidateIndex < heroMark.candidates.length)
                    Qt.callLater(function() { heroMark.candidateIndex++ })
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.centerIn: parent
                  visible: heroImage.status !== Image.Ready
                  text: root.barIcon
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
            }
          }

          // ---------- Problem card ----------
          BorderSurface {
            visible: root.hasProblem
            width: parent.width
            implicitHeight: problemText.implicitHeight + Style.spacing.xl * 2
            color: root.alpha(root.urgent, 0.10)
            borderSpec: Border.flat(root.alpha(root.urgent, 0.35), 1)
            radius: Style.cornerRadius

            Text {
              id: problemText
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              text: {
                if (!root.record) return ""
                var lines = []
                if (String(root.record.error || "") !== "") lines.push(root.record.error)
                if (String(root.record.authHelp || "") !== "") lines.push(root.record.authHelp)
                return lines.join("\n")
              }
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Text {
            visible: !root.record
            width: parent.width
            topPadding: Style.space(16)
            text: collectProcess.running ? "Checking ollama.com…" : "No usage yet. Press r to check."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          // ---------- Limits ----------
          PanelSeparator {
            visible: limitsSection.visible
            foreground: root.foreground
          }

          Column {
            id: limitsSection
            visible: root.windows.length > 0
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "LIMITS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.windows

              LimitRow {
                required property var modelData
                width: limitsSection.width
                window: modelData
              }
            }
          }

          // ---------- Requests by model, one section per window ----------
          Repeater {
            model: root.windows

            Column {
              id: windowSection
              required property var modelData
              readonly property var rows: root.orderedRows(modelData)
              readonly property real peak: root.modelPeak(modelData)

              visible: rows.length > 0
              width: column.width
              spacing: Style.spacing.md

              PanelSeparator {
                foreground: root.foreground
              }

              Item {
                width: parent.width
                implicitHeight: sectionHeader.implicitHeight

                PanelSectionHeader {
                  id: sectionHeader
                  anchors.left: parent.left
                  text: String(windowSection.modelData.title || "").toUpperCase() + " · REQUESTS"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.right: parent.right
                  anchors.verticalCenter: sectionHeader.verticalCenter
                  text: Number(windowSection.modelData.requests || 0) + " total"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Repeater {
                model: windowSection.rows

                ModelRow {
                  required property var modelData
                  width: windowSection.width
                  row: modelData
                  share: modelData.tool ? 0 : Number(modelData.requests || 0) / windowSection.peak
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.windows.length > 0
            width: parent.width
            topPadding: Style.space(2)
            text: "Request counts from ollama.com · r refresh"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
          }
        }
      }
    }
  }

  // ----------------------------------------------------- components
  // Copied from the stock agents panel (inline there, not exported by qs.Ui).

  component LimitRow: Column {
    id: limitRow
    property var window: null
    readonly property bool alarming: !!window && Number(window.percent) >= 0.9

    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: Math.max(limitLabel.implicitHeight, limitValue.implicitHeight)

      Text {
        id: limitLabel
        textFormat: Text.PlainText
        text: limitRow.window ? String(limitRow.window.title || "") : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        anchors.left: parent.left
        anchors.right: limitValue.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: limitValue
        textFormat: Text.PlainText
        text: root.percentText(limitRow.window)
        color: limitRow.alarming ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Meter {
      width: parent.width
      value: limitRow.window ? Number(limitRow.window.percent || 0) : -1
      alarming: limitRow.alarming
    }
  }

  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1)
      color: meter.alarming ? root.urgent : root.foreground

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }
  }

  // Share bar fills the row behind the label; tools get no bar and a dim label.
  component ModelRow: Item {
    id: modelRow
    property var row: null
    property real share: 0
    readonly property bool tool: !!row && row.tool === true

    implicitHeight: modelName.implicitHeight + Style.spacing.lg

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.05)
    }

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: parent.width * root.clamp(modelRow.share, 0, 1)
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.14)

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }

    Text {
      id: modelName
      textFormat: Text.PlainText
      text: modelRow.row ? (modelRow.tool ? modelRow.row.name + " (tool)" : modelRow.row.name) : ""
      color: modelRow.tool ? root.dim : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: modelCount.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: modelCount
      textFormat: Text.PlainText
      text: modelRow.row ? String(modelRow.row.requests || 0) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
