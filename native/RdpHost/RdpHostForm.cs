using System;
using System.Drawing;
using System.Windows.Forms;
using DrawingSize = System.Drawing.Size;

namespace RdpHost;

/// <summary>
/// Borderless top-level window that hosts the <see cref="RdpActiveXHost"/>. Its HWND
/// is what ok-ww reparents into the Qt UI. All members are expected to be invoked on
/// the UI thread (the pipe handler marshals via <see cref="Control.Invoke(Delegate)"/>).
/// </summary>
internal sealed class RdpHostForm : Form
{
    private readonly RdpActiveXHost _rdpHost = new();
    private readonly System.Windows.Forms.Timer _focusTimer = new();
    private bool _lastInputFocused;

    internal event EventHandler? LoginCompleted;
    internal event EventHandler<RdpConnectionFailedEventArgs>? ConnectionFailed;
    internal event EventHandler<(int Width, int Height)>? RemoteDesktopSizeChanged;
    internal event EventHandler<bool>? InputWindowFocusChanged;
    internal event EventHandler<bool>? MouseInputModeChanged;

    internal RdpHostForm(int width, int height)
    {
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        BackColor = Color.Black;
        ClientSize = new DrawingSize(Math.Max(width, 200), Math.Max(height, 200));
        Text = "ok-ww RDP Host";

        _rdpHost.Dock = DockStyle.Fill;
        Controls.Add(_rdpHost);

        _rdpHost.LoginCompleted += (_, _) => LoginCompleted?.Invoke(this, EventArgs.Empty);
        _rdpHost.ConnectionFailed += (_, e) => ConnectionFailed?.Invoke(this, e);
        _rdpHost.RemoteDesktopSizeChanged += (_, size) => RemoteDesktopSizeChanged?.Invoke(this, size);
        _rdpHost.MouseInputModeChanged += (_, relative) => MouseInputModeChanged?.Invoke(this, relative);

        _focusTimer.Interval = 200;
        _focusTimer.Tick += (_, _) => PollInputFocus();
        _focusTimer.Start();
    }

    /// <summary>HWND ok-ww embeds. Valid once the form handle exists.</summary>
    internal IntPtr HostHandle => Handle;

    internal int ConnectedState => _rdpHost.ConnectedState;

    internal bool InputFocused => _rdpHost.IsInputWindowFocused();

    internal void Connect(int width, int height, bool smartSizing, bool audioMuted, bool sendSystemShortcuts)
    {
        _rdpHost.SetSmartSizing(smartSizing);
        _rdpHost.SetAudioMuted(audioMuted);
        _rdpHost.SetSendSystemShortcutsToRemote(sendSystemShortcuts);
        _rdpHost.ConnectToChildSession(new DrawingSize(width, height));
    }

    internal void Disconnect() => _rdpHost.DisconnectSession();

    internal void Reconnect(int width, int height) =>
        _rdpHost.ReconnectToChildSession(new DrawingSize(width, height));

    internal void SetSmartSizing(bool enabled) => _rdpHost.SetSmartSizing(enabled);

    internal void SetAudioMuted(bool muted) => _rdpHost.SetAudioMuted(muted);

    internal void SetSendSystemShortcuts(bool enabled) => _rdpHost.SetSendSystemShortcutsToRemote(enabled);

    internal void SendShowDesktopShortcut() => _rdpHost.SendShowDesktopShortcut();

    internal void SendTaskViewShortcut() => _rdpHost.SendTaskViewShortcut();

    internal bool FocusInput() => _rdpHost.FocusInputWindow();

    internal void SendKeys((int Scan, bool Up, bool Extended)[] keys) => _rdpHost.SendKeysPublic(keys);

    internal void LogoffChildSession()
    {
        try
        {
            _rdpHost.DisconnectSession();
        }
        catch (Exception)
        {
            // ignore, session logoff below is the important part
        }

        ChildSessionNativeMethods.TerminateChildSession(wait: true);
    }

    private void PollInputFocus()
    {
        if (!IsHandleCreated)
        {
            return;
        }

        bool focused;
        try
        {
            focused = _rdpHost.IsInputWindowFocused();
        }
        catch (Exception)
        {
            return;
        }

        if (focused != _lastInputFocused)
        {
            _lastInputFocused = focused;
            InputWindowFocusChanged?.Invoke(this, focused);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _focusTimer.Dispose();
        }

        base.Dispose(disposing);
    }
}
