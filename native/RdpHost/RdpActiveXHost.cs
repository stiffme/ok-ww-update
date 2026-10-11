using System;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using DrawingSize = System.Drawing.Size;

namespace RdpHost;

internal sealed class RdpConnectionFailedEventArgs : EventArgs
{
    internal RdpConnectionFailedEventArgs(string message, int errorCode, int? extendedErrorCode = null)
    {
        Message = message;
        ErrorCode = errorCode;
        ExtendedErrorCode = extendedErrorCode;
    }

    internal string Message { get; }

    internal int ErrorCode { get; }

    internal int? ExtendedErrorCode { get; }
}

/// <summary>
/// WinForms ActiveX host for the RDP client control (MsRdpClient10), connected to
/// the local child session. Ported from BetterGenshinImpact
/// View/Controls/ChildSession/RdpActiveXHost.cs.
/// </summary>
internal sealed class RdpActiveXHost : AxHost
{
    // Windows 10+ built-in non-scriptable RDP ActiveX control (MsRdpClient10).
    private const string RdpClientClsid = "A0C63C30-F08D-4AB4-907C-34905D770C7D";
    private const short VariantFalse = 0;
    private const short VariantTrue = -1;
    private const int RedirectAudioToClient = 0;
    private const int DisableRemoteAudio = 2;

    private static readonly RemoteKey LeftWindowsKey = new(0x5B, IsExtended: true);
    private static readonly RemoteKey DKey = new(0x20, IsExtended: false);
    private static readonly RemoteKey TabKey = new(0x0F, IsExtended: false);

    private ConnectionPointCookie? _eventCookie;
    private RdpEventSink? _eventSink;
    private bool _connectionAttemptInProgress;
    private bool _connectionFailureReported;
    private RdpConnectionFailedEventArgs? _lastConnectionDiagnostic;
    private bool _disconnectRequested;
    private bool _sendSystemShortcutsToRemote = true;
    private bool _audioMuted;
    private DrawingSize? _pendingReconnectDesktopSize;
    private bool _smartSizingEnabled = true;

    internal event EventHandler<RdpConnectionFailedEventArgs>? ConnectionFailed;

    internal event EventHandler? LoginCompleted;

    internal event EventHandler<(int Width, int Height)>? RemoteDesktopSizeChanged;

    internal event EventHandler<bool>? MouseInputModeChanged;

    internal RdpConnectionFailedEventArgs? LastConnectionDiagnostic => _lastConnectionDiagnostic;

    internal RdpActiveXHost()
        : base(RdpClientClsid)
    {
        Dock = DockStyle.Fill;
    }

    internal int ConnectedState
    {
        get
        {
            if (!IsHandleCreated)
            {
                return 0;
            }

            return Convert.ToInt32(GetComProperty(GetRequiredOcx(), "Connected"), CultureInfo.InvariantCulture);
        }
    }

    internal void ConnectToChildSession(DrawingSize desktopSize)
    {
        if (ConnectedState != 0)
        {
            return;
        }

        ChildSessionNativeMethods.ClearRdpInputWindowCache(Handle);
        var client = GetRequiredOcx();
        var width = Math.Clamp(desktopSize.Width, 200, 8192);
        var height = Math.Clamp(desktopSize.Height, 200, 8192);

        SetComProperty(client, "Server", "localhost");
        SetComProperty(client, "DesktopWidth", width);
        SetComProperty(client, "DesktopHeight", height);
        SetComProperty(client, "ColorDepth", 32);
        SetComProperty(client, "ConnectingText", "正在创建 ok-ww 桌面分身...");
        SetComProperty(client, "DisconnectedText", "ok-ww 桌面分身已断开");

        var securedSettings = GetComProperty(client, "SecuredSettings2")
            ?? throw new COMException("RDP ActiveX 未返回 SecuredSettings2。");
        RunComStep("设置系统组合键发送位置", () =>
            SetComProperty(securedSettings, "KeyboardHookMode", _sendSystemShortcutsToRemote ? 1 : 0));
        RunComStep("设置远程音频重定向", () =>
            SetComProperty(securedSettings, "AudioRedirectionMode", _audioMuted ? DisableRemoteAudio : RedirectAudioToClient));

        var advancedSettings = GetComProperty(client, "AdvancedSettings7")
            ?? throw new COMException("RDP ActiveX 未返回 AdvancedSettings7。");
        RunComStep("设置 RDP 连接端口", () =>
            SetComProperty(advancedSettings, "RDPPort", ChildSessionNativeMethods.GetConfiguredRdpPort()));
        RunComStep("启用 CredSSP", () => SetComProperty(advancedSettings, "EnableCredSspSupport", true));
        RunComStep("启用远程 Windows 键", () => SetComProperty(advancedSettings, "EnableWindowsKey", 1));
        RunComStep("设置显示缩放", () => SetComProperty(advancedSettings, "SmartSizing", _smartSizingEnabled));

        object connectToChildSession = true;
        RunComStep("设置 ConnectToChildSession", () =>
        {
            var extendedSettings = (IMsRdpExtendedSettings)client;
            TrySetExtendedProperty(extendedSettings, "EnableZoom", true);
            // 相对鼠标模式（Win11 24H2+）。开启后，当远端会话隐藏光标（游戏捕获鼠标、
            // 进入 raw input）时，客户端发送「相对位移」而不是绝对坐标，游戏因此拿到
            // 1:1 的视角增量，避免绝对坐标换算被 DPI/缩放/加速放大导致的「视角飞快」。
            // 相对增量与 SmartSizing 的画面缩放无关，所以缩放照样全屏、视角依然 1:1。
            // 旧系统不支持该属性时静默跳过（TrySetExtendedProperty 吞掉 COMException）。
            TrySetExtendedProperty(extendedSettings, "AllowRelativeMouseMode", true);
            extendedSettings.set_Property("ConnectToChildSession", ref connectToChildSession);
        });

        _connectionAttemptInProgress = true;
        _connectionFailureReported = false;
        _lastConnectionDiagnostic = null;
        _disconnectRequested = false;
        try
        {
            RunComStep("调用 RDP Connect()", () => InvokeComMethod(client, "Connect"));
        }
        catch
        {
            _connectionAttemptInProgress = false;
            throw;
        }
    }

    internal void ReconnectToChildSession(DrawingSize desktopSize)
    {
        if (_disconnectRequested || _pendingReconnectDesktopSize.HasValue)
        {
            _pendingReconnectDesktopSize = desktopSize;
            return;
        }

        if (ConnectedState == 0)
        {
            ConnectToChildSession(desktopSize);
            return;
        }

        _pendingReconnectDesktopSize = desktopSize;
        try
        {
            if (!DisconnectCore())
            {
                _pendingReconnectDesktopSize = null;
                ConnectToChildSession(desktopSize);
            }
        }
        catch
        {
            _pendingReconnectDesktopSize = null;
            throw;
        }
    }

    internal void DisconnectSession()
    {
        _pendingReconnectDesktopSize = null;
        _ = DisconnectCore();
    }

    internal void SetSmartSizing(bool enabled)
    {
        _smartSizingEnabled = enabled;
        if (!IsHandleCreated)
        {
            return;
        }

        var advancedSettings = GetComProperty(GetRequiredOcx(), "AdvancedSettings7")
            ?? throw new COMException("RDP ActiveX 未返回 AdvancedSettings7。");
        RunComStep("设置显示缩放", () => SetComProperty(advancedSettings, "SmartSizing", enabled));
    }

    internal void SetSendSystemShortcutsToRemote(bool enabled)
    {
        _sendSystemShortcutsToRemote = enabled;
    }

    internal void SetAudioMuted(bool muted)
    {
        _audioMuted = muted;
    }

    internal void SendShowDesktopShortcut()
    {
        SendShortcut(new[]
        {
            new KeyStroke(LeftWindowsKey, IsKeyUp: false),
            new KeyStroke(DKey, IsKeyUp: false),
            new KeyStroke(DKey, IsKeyUp: true),
            new KeyStroke(LeftWindowsKey, IsKeyUp: true)
        }, "Win+D");
    }

    internal void SendTaskViewShortcut()
    {
        SendShortcut(new[]
        {
            new KeyStroke(LeftWindowsKey, IsKeyUp: false),
            new KeyStroke(TabKey, IsKeyUp: false),
            new KeyStroke(TabKey, IsKeyUp: true),
            new KeyStroke(LeftWindowsKey, IsKeyUp: true)
        }, "Win+Tab");
    }

    internal bool IsInputWindowFocused()
    {
        return ConnectedState == 1 && ChildSessionNativeMethods.IsRdpInputWindowFocused(Handle);
    }

    internal bool FocusInputWindow()
    {
        return ConnectedState == 1 && ChildSessionNativeMethods.TryFocusRdpInputWindow(Handle);
    }

    protected override void OnHandleDestroyed(EventArgs e)
    {
        if (IsHandleCreated)
        {
            ChildSessionNativeMethods.ClearRdpInputWindowCache(Handle);
        }

        base.OnHandleDestroyed(e);
    }

    protected override void CreateSink()
    {
        base.CreateSink();
        _eventSink = new RdpEventSink(this);
        _eventCookie = new ConnectionPointCookie(GetOcx()!, _eventSink, typeof(IMsTscAxEvents));
    }

    protected override void DetachSink()
    {
        try
        {
            _eventCookie?.Disconnect();
            _eventCookie = null;
            _eventSink = null;
        }
        finally
        {
            base.DetachSink();
        }
    }

    private bool DisconnectCore()
    {
        if (IsHandleCreated)
        {
            ChildSessionNativeMethods.ClearRdpInputWindowCache(Handle);
        }

        if (ConnectedState == 0)
        {
            return false;
        }

        _disconnectRequested = true;
        try
        {
            InvokeComMethod(GetRequiredOcx(), "Disconnect");
            return true;
        }
        catch
        {
            _disconnectRequested = false;
            throw;
        }
    }

    private void SendShortcut(KeyStroke[] strokes, string displayName)
    {
        if (ConnectedState != 1)
        {
            throw new InvalidOperationException($"桌面分身尚未完全连接，无法发送 {displayName}。");
        }

        if (!ChildSessionNativeMethods.TryFocusRdpInputWindow(Handle))
        {
            throw new InvalidOperationException("无法将键盘焦点切换到桌面分身。");
        }

        RunComStep($"向 Child Session 发送 {displayName}", () => SendKeyStrokes(strokes));
    }

    internal void SendKeysPublic((int Scan, bool Up, bool Extended)[] keys)
    {
        if (ConnectedState != 1)
        {
            throw new InvalidOperationException("桌面分身尚未完全连接，无法发送按键。");
        }

        var strokes = new KeyStroke[keys.Length];
        for (var index = 0; index < keys.Length; index++)
        {
            strokes[index] = new KeyStroke(new RemoteKey(keys[index].Scan, keys[index].Extended), keys[index].Up);
        }

        SendKeyStrokes(strokes);
    }

    private void SendKeyStrokes(KeyStroke[] strokes)
    {
        var keyUpStates = new short[strokes.Length];
        var keyData = new int[strokes.Length];
        for (var index = 0; index < strokes.Length; index++)
        {
            keyUpStates[index] = strokes[index].IsKeyUp ? VariantTrue : VariantFalse;
            keyData[index] = strokes[index].Key.ScanCode | (strokes[index].Key.IsExtended ? 0x0100 : 0);
        }

        var nonScriptableClient = (IMsRdpClientNonScriptable)GetRequiredOcx();
        nonScriptableClient.SendKeys(strokes.Length, ref keyUpStates[0], ref keyData[0]);
    }

    private void OnLoginComplete()
    {
        _connectionAttemptInProgress = false;
        _connectionFailureReported = false;
        _lastConnectionDiagnostic = null;
        LoginCompleted?.Invoke(this, EventArgs.Empty);
    }

    private void OnDisconnected(int disconnectReason)
    {
        var failedWhileConnecting = _connectionAttemptInProgress;
        _connectionAttemptInProgress = false;

        if (_disconnectRequested)
        {
            _disconnectRequested = false;
            if (_pendingReconnectDesktopSize.HasValue && !IsDisposed && !Disposing)
            {
                try
                {
                    BeginInvoke(new Action(ConnectPendingReconnect));
                }
                catch (InvalidOperationException)
                {
                    _pendingReconnectDesktopSize = null;
                }
            }

            return;
        }

        var extendedDisconnectReason = TryGetExtendedDisconnectReason();
        if (_connectionFailureReported
            || (!failedWhileConnecting && IsNormalDisconnect(disconnectReason, extendedDisconnectReason)))
        {
            return;
        }

        var errorDescription = TryGetErrorDescription(disconnectReason, extendedDisconnectReason);
        var failureTitle = failedWhileConnecting ? "RDP 连接失败" : "RDP 连接意外断开";
        var message = string.IsNullOrWhiteSpace(errorDescription)
            ? $"{failureTitle}。\n\n断开原因：{FormatErrorCode(disconnectReason)}\n扩展原因：{FormatErrorCode(extendedDisconnectReason)}"
            : $"{failureTitle}：{errorDescription}\n\n断开原因：{FormatErrorCode(disconnectReason)}\n扩展原因：{FormatErrorCode(extendedDisconnectReason)}";
        ReportConnectionFailure(message, disconnectReason, extendedDisconnectReason);
    }

    private async void ConnectPendingReconnect()
    {
        await System.Threading.Tasks.Task.Delay(TimeSpan.FromSeconds(1));
        var desktopSize = _pendingReconnectDesktopSize;
        _pendingReconnectDesktopSize = null;
        if (!desktopSize.HasValue || IsDisposed || Disposing)
        {
            return;
        }

        try
        {
            ConnectToChildSession(desktopSize.Value);
        }
        catch (Exception exception) when (exception is COMException or TargetInvocationException or InvalidOperationException)
        {
            var actualException = exception.GetBaseException();
            var errorCode = actualException is COMException comException ? comException.ErrorCode : 0;
            ReportConnectionFailure($"RDP 自动重新连接失败：{actualException.Message}", errorCode);
        }
    }

    private void OnFatalError(int errorCode)
    {
        _connectionAttemptInProgress = false;
        if (_disconnectRequested)
        {
            return;
        }

        ReportConnectionFailure($"RDP 客户端发生致命错误。\n\n错误代码：{FormatErrorCode(errorCode)}", errorCode);
    }

    private void OnLogonError(int errorCode)
    {
        if (_disconnectRequested)
        {
            return;
        }

        var message = $"RDP 登录阶段错误。\n\n错误代码：{FormatErrorCode(errorCode)}";
        if (IsNonTerminalLogonEvent(errorCode))
        {
            _lastConnectionDiagnostic = new RdpConnectionFailedEventArgs(message, errorCode);
            return;
        }

        _connectionAttemptInProgress = false;
        ReportConnectionFailure(message, errorCode);
    }

    private void OnRemoteDesktopSizeChange(int width, int height)
    {
        RemoteDesktopSizeChanged?.Invoke(this, (width, height));
    }

    private void OnMouseInputModeChanged(bool isRelativeMouseMode)
    {
        MouseInputModeChanged?.Invoke(this, isRelativeMouseMode);
    }

    private int TryGetExtendedDisconnectReason()
    {
        try
        {
            return Convert.ToInt32(GetComProperty(GetRequiredOcx(), "ExtendedDisconnectReason"), CultureInfo.InvariantCulture);
        }
        catch (Exception exception) when (exception is COMException or TargetInvocationException or InvalidOperationException)
        {
            return 0;
        }
    }

    private string? TryGetErrorDescription(int disconnectReason, int extendedDisconnectReason)
    {
        try
        {
            return Convert.ToString(InvokeComMethod(GetRequiredOcx(), "GetErrorDescription", disconnectReason, extendedDisconnectReason), CultureInfo.CurrentCulture);
        }
        catch (Exception exception) when (exception is COMException or TargetInvocationException or InvalidOperationException)
        {
            return null;
        }
    }

    private void ReportConnectionFailure(string message, int errorCode, int? extendedErrorCode = null)
    {
        if (_connectionFailureReported)
        {
            return;
        }

        _connectionFailureReported = true;
        ConnectionFailed?.Invoke(this, new RdpConnectionFailedEventArgs(message, errorCode, extendedErrorCode));
    }

    private static bool IsNormalDisconnect(int disconnectReason, int extendedDisconnectReason)
    {
        return disconnectReason is 1 or 2 or 3 && extendedDisconnectReason is 0 or 1 or 2;
    }

    private static string FormatErrorCode(int errorCode)
    {
        return $"{errorCode} (0x{unchecked((uint)errorCode):X8})";
    }

    private static bool IsNonTerminalLogonEvent(int errorCode)
    {
        return errorCode is -5 or -4 or -2 or 3;
    }

    private object GetRequiredOcx()
    {
        if (!IsHandleCreated)
        {
            _ = Handle;
        }

        return GetOcx() ?? throw new InvalidOperationException("RDP ActiveX 控件尚未完成初始化，无法访问 COM 实例。");
    }

    private static object? GetComProperty(object target, string propertyName)
    {
        return target.GetType().InvokeMember(propertyName, BindingFlags.GetProperty, binder: null, target, args: null, CultureInfo.InvariantCulture);
    }

    private static void SetComProperty(object target, string propertyName, object value)
    {
        target.GetType().InvokeMember(propertyName, BindingFlags.SetProperty, binder: null, target, new[] { value }, CultureInfo.InvariantCulture);
    }

    private static void TrySetExtendedProperty(IMsRdpExtendedSettings extendedSettings, string propertyName, object value)
    {
        try
        {
            extendedSettings.set_Property(propertyName, ref value);
        }
        catch (COMException)
        {
            // Older MsTscAx builds may not support this extended property.
        }
    }

    private static object? InvokeComMethod(object target, string methodName, params object[]? args)
    {
        return target.GetType().InvokeMember(methodName, BindingFlags.InvokeMethod, binder: null, target, args, CultureInfo.InvariantCulture);
    }

    private static void RunComStep(string stepName, Action action)
    {
        try
        {
            action();
        }
        catch (Exception exception)
        {
            var actualException = exception.GetBaseException();
            if (actualException is COMException comException)
            {
                throw new COMException($"{stepName}失败：{comException.Message}", comException.ErrorCode);
            }

            throw;
        }
    }

    [ComImport]
    [Guid("336D5562-EFA8-482E-8CB3-C5C0FC7A7DB6")]
    [InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
    [TypeLibType(TypeLibTypeFlags.FDispatchable)]
    private interface IMsTscAxEvents
    {
        [DispId(1)] void OnConnecting();
        [DispId(2)] void OnConnected();
        [DispId(3)] void OnLoginComplete();
        [DispId(4)] void OnDisconnected([In] int disconnectReason);
        [DispId(5)] void OnEnterFullScreenMode();
        [DispId(6)] void OnLeaveFullScreenMode();
        [DispId(7)] void OnChannelReceivedData([In, MarshalAs(UnmanagedType.BStr)] string channelName, [In, MarshalAs(UnmanagedType.BStr)] string data);
        [DispId(8)] void OnRequestGoFullScreen();
        [DispId(9)] void OnRequestLeaveFullScreen();
        [DispId(10)] void OnFatalError([In] int errorCode);
        [DispId(11)] void OnWarning([In] int warningCode);
        [DispId(12)] void OnRemoteDesktopSizeChange([In] int width, [In] int height);
        [DispId(13)] void OnIdleTimeoutNotification();
        [DispId(14)] void OnRequestContainerMinimize();
        [DispId(15)] void OnConfirmClose([Out] out bool allowClose);
        [DispId(16)] void OnReceivedTSPublicKey([In, MarshalAs(UnmanagedType.BStr)] string publicKey, [Out] out bool continueLogon);
        [DispId(17)] void OnAutoReconnecting([In] int disconnectReason, [In] int attemptCount, [Out] out AutoReconnectContinueState continueStatus);
        [DispId(18)] void OnAuthenticationWarningDisplayed();
        [DispId(19)] void OnAuthenticationWarningDismissed();
        [DispId(20)] void OnRemoteProgramResult([In, MarshalAs(UnmanagedType.BStr)] string remoteProgram, [In] RemoteProgramResult error, [In] bool isExecutable);
        [DispId(21)] void OnRemoteProgramDisplayed([In] bool displayed, [In] uint displayInformation);
        [DispId(29)] void OnRemoteWindowDisplayed([In] bool displayed, [In] ref RemotableHandle windowHandle, [In] RemoteWindowDisplayedAttribute windowAttribute);
        [DispId(22)] void OnLogonError([In] int errorCode);
        [DispId(23)] void OnFocusReleased([In] int direction);
        [DispId(24)] void OnUserNameAcquired([In, MarshalAs(UnmanagedType.BStr)] string userName);
        [DispId(26)] void OnMouseInputModeChanged([In] bool isRelativeMouseMode);
        [DispId(28)] void OnServiceMessageReceived([In, MarshalAs(UnmanagedType.BStr)] string serviceMessage);
        [DispId(30)] void OnConnectionBarPullDown();
        [DispId(32)] void OnNetworkStatusChanged([In] uint qualityLevel, [In] int bandwidth, [In] int roundTripTime);
        [DispId(35)] void OnDevicesButtonPressed();
        [DispId(33)] void OnAutoReconnected();
        [DispId(34)] void OnAutoReconnecting2([In] int disconnectReason, [In] bool networkAvailable, [In] int attemptCount, [In] int maxAttemptCount);
    }

    [ComVisible(true)]
    [ClassInterface(ClassInterfaceType.None)]
    private sealed class RdpEventSink(RdpActiveXHost owner) : IMsTscAxEvents
    {
        public void OnConnecting() { }
        public void OnConnected() { }
        public void OnLoginComplete() => owner.OnLoginComplete();
        public void OnDisconnected(int disconnectReason) => owner.OnDisconnected(disconnectReason);
        public void OnEnterFullScreenMode() { }
        public void OnLeaveFullScreenMode() { }
        public void OnChannelReceivedData(string channelName, string data) { }
        public void OnRequestGoFullScreen() { }
        public void OnRequestLeaveFullScreen() { }
        public void OnFatalError(int errorCode) => owner.OnFatalError(errorCode);
        public void OnWarning(int warningCode) { }
        public void OnRemoteDesktopSizeChange(int width, int height) => owner.OnRemoteDesktopSizeChange(width, height);
        public void OnIdleTimeoutNotification() { }
        public void OnRequestContainerMinimize() { }
        public void OnConfirmClose(out bool allowClose) => allowClose = true;
        public void OnReceivedTSPublicKey(string publicKey, out bool continueLogon) => continueLogon = true;
        public void OnAutoReconnecting(int disconnectReason, int attemptCount, out AutoReconnectContinueState continueStatus) => continueStatus = AutoReconnectContinueState.Automatic;
        public void OnAuthenticationWarningDisplayed() { }
        public void OnAuthenticationWarningDismissed() { }
        public void OnRemoteProgramResult(string remoteProgram, RemoteProgramResult error, bool isExecutable) { }
        public void OnRemoteProgramDisplayed(bool displayed, uint displayInformation) { }
        public void OnRemoteWindowDisplayed(bool displayed, ref RemotableHandle windowHandle, RemoteWindowDisplayedAttribute windowAttribute) { }
        public void OnLogonError(int errorCode) => owner.OnLogonError(errorCode);
        public void OnFocusReleased(int direction) { }
        public void OnUserNameAcquired(string userName) { }
        public void OnMouseInputModeChanged(bool isRelativeMouseMode) => owner.OnMouseInputModeChanged(isRelativeMouseMode);
        public void OnServiceMessageReceived(string serviceMessage) { }
        public void OnConnectionBarPullDown() { }
        public void OnNetworkStatusChanged(uint qualityLevel, int bandwidth, int roundTripTime) { }
        public void OnDevicesButtonPressed() { }
        public void OnAutoReconnected() => owner.OnLoginComplete();
        public void OnAutoReconnecting2(int disconnectReason, bool networkAvailable, int attemptCount, int maxAttemptCount) { }
    }

    private enum AutoReconnectContinueState { Automatic, Stop, Manual }

    private enum RemoteProgramResult
    {
        Ok, Locked, ProtocolError, NotInWhitelist, NetworkPathDenied, FileNotFound, Failure, HookNotLoaded
    }

    private enum RemoteWindowDisplayedAttribute { None, WindowDisplayed, ShellIconDisplayed }

    [StructLayout(LayoutKind.Sequential)]
    private struct RemotableHandle
    {
        internal int Context;
        internal RemotableHandleUnion Value;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct RemotableHandleUnion
    {
        [FieldOffset(0)] internal int InProcessHandle;
        [FieldOffset(0)] internal int RemoteHandle;
    }

    [ComImport]
    [Guid("302D8188-0052-4807-806A-362B628F9AC5")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMsRdpExtendedSettings
    {
        void set_Property([In, MarshalAs(UnmanagedType.BStr)] string propertyName, [In, MarshalAs(UnmanagedType.Struct)] ref object value);

        [return: MarshalAs(UnmanagedType.Struct)]
        object get_Property([In, MarshalAs(UnmanagedType.BStr)] string propertyName);
    }

    [ComImport]
    [Guid("2F079C4C-87B2-4AFD-97AB-20CDB43038AE")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMsRdpClientNonScriptable
    {
        void put_ClearTextPassword([In, MarshalAs(UnmanagedType.BStr)] string value);
        void put_PortablePassword([In, MarshalAs(UnmanagedType.BStr)] string value);
        [return: MarshalAs(UnmanagedType.BStr)] string get_PortablePassword();
        void put_PortableSalt([In, MarshalAs(UnmanagedType.BStr)] string value);
        [return: MarshalAs(UnmanagedType.BStr)] string get_PortableSalt();
        void put_BinaryPassword([In, MarshalAs(UnmanagedType.BStr)] string value);
        [return: MarshalAs(UnmanagedType.BStr)] string get_BinaryPassword();
        void put_BinarySalt([In, MarshalAs(UnmanagedType.BStr)] string value);
        [return: MarshalAs(UnmanagedType.BStr)] string get_BinarySalt();
        void ResetPassword();
        void NotifyRedirectDeviceChange(nuint wParam, nint lParam);
        void SendKeys(int numKeys, [In] ref short keyUpStates, [In] ref int keyData);
    }

    private readonly record struct RemoteKey(int ScanCode, bool IsExtended);

    private readonly record struct KeyStroke(RemoteKey Key, bool IsKeyUp);
}
