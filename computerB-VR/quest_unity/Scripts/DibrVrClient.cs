using System;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.XR;

/// <summary>
/// Quest / OpenXR：将头部位姿经 WebSocket JSON 发给渲染服务器，并接收双目 JPEG 显示在 RawImage 上。
/// 把本脚本挂到场景中的管理物体；LeftDisplay / RightDisplay 指向两个 RawImage（或直接改线存 RenderTexture）。
/// Player Settings 需启用 XR Plug-in（Oculus / OpenXR），构建 Android 到设备。
/// </summary>
public class DibrVrClient : MonoBehaviour
{
    [SerializeField] private string serverUrl = "ws://192.168.1.10:8765/";
    [SerializeField] private RawImage leftDisplay;
    [SerializeField] private RawImage rightDisplay;

    private ClientWebSocket _ws;
    private CancellationTokenSource _cts;
    private Texture2D _texL;
    private Texture2D _texR;
    private readonly object _lock = new object();
    private byte[] _pendingL;
    private byte[] _pendingR;

    private async void Start()
    {
        _cts = new CancellationTokenSource();
        _ = RunSocket(_cts.Token);
    }

    private async Task RunSocket(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                _ws = new ClientWebSocket();
                var uri = new Uri(serverUrl);
                await _ws.ConnectAsync(uri, ct);
                Debug.Log("DIBR WebSocket connected");
                var sendTask = SendPosesLoop(ct);
                var recvTask = RecvLoop(ct);
                await Task.WhenAny(sendTask, recvTask);
            }
            catch (Exception ex)
            {
                Debug.LogWarning("DIBR socket error: " + ex.Message);
            }
            finally
            {
                if (_ws != null)
                {
                    try { await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None); } catch { }
                    _ws.Dispose();
                    _ws = null;
                }
            }
            await Task.Delay(2000, ct);
        }
    }

    private async Task SendPosesLoop(CancellationToken ct)
    {
        while (_ws != null && _ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            if (TryGetHeadPose(out var p, out var q))
            {
                var json =
                    "{\"t_ns\":" + DateTime.UtcNow.Ticks * 100 +
                    ",\"pos\":[" + p.x + "," + p.y + "," + p.z +
                    "],\"quat\":[" + q.x + "," + q.y + "," + q.z + "," + q.w + "]}";
                var b = Encoding.UTF8.GetBytes(json);
                await _ws.SendAsync(new ArraySegment<byte>(b), WebSocketMessageType.Text, true, ct);
            }
            await Task.Yield();
        }
    }

    private static bool TryGetHeadPose(out Vector3 pos, out Quaternion rot)
    {
        var dev = InputDevices.GetDeviceAtXRNode(XRNode.Head);
        if (dev.isValid &&
            dev.TryGetFeatureValue(CommonUsages.devicePosition, out pos) &&
            dev.TryGetFeatureValue(CommonUsages.deviceRotation, out rot))
        {
            return true;
        }
        pos = Vector3.zero;
        rot = Quaternion.identity;
        return false;
    }

    private async Task RecvLoop(CancellationToken ct)
    {
        var buf = new byte[1024 * 1024];
        while (_ws != null && _ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            var ms = new MemoryStream();
            WebSocketReceiveResult res;
            do
            {
                res = await _ws.ReceiveAsync(new ArraySegment<byte>(buf), ct);
                if (res.MessageType == WebSocketMessageType.Close) return;
                if (res.MessageType == WebSocketMessageType.Binary)
                    ms.Write(buf, 0, res.Count);
            } while (!res.EndOfMessage);

            var data = ms.ToArray();
            if (TryParseStereoJpeg(data, out var jl, out var jr))
            {
                lock (_lock)
                {
                    _pendingL = jl;
                    _pendingR = jr;
                }
            }
        }
    }

    private static bool TryParseStereoJpeg(byte[] data, out byte[] left, out byte[] right)
    {
        left = right = null;
        const int hdr = 48;
        if (data == null || data.Length < hdr) return false;
        if (data[0] != (byte)'D' || data[1] != (byte)'I' || data[2] != (byte)'B' || data[3] != (byte)'R') return false;
        if (BitConverter.ToUInt16(data, 4) != 1) return false;
        uint ll = BitConverter.ToUInt32(data, 40);
        uint rl = BitConverter.ToUInt32(data, 44);
        int off = hdr;
        if (off + ll + rl != data.Length) return false;
        left = new byte[ll];
        Buffer.BlockCopy(data, off, left, 0, (int)ll);
        off += (int)ll;
        right = new byte[rl];
        Buffer.BlockCopy(data, off, right, 0, (int)rl);
        return true;
    }

    private void Update()
    {
        byte[] l, r;
        lock (_lock)
        {
            l = _pendingL;
            r = _pendingR;
            _pendingL = null;
            _pendingR = null;
        }
        if (l != null) ApplyJpegToRawImage(l, ref _texL, leftDisplay);
        if (r != null) ApplyJpegToRawImage(r, ref _texR, rightDisplay);
    }

    private static void ApplyJpegToRawImage(byte[] jpeg, ref Texture2D tex, RawImage target)
    {
        if (target == null || jpeg == null || jpeg.Length == 0) return;
        if (tex == null) tex = new Texture2D(2, 2, TextureFormat.RGB24, false);
        if (tex.LoadImage(jpeg))
        {
            target.texture = tex;
        }
    }

    private void OnDestroy()
    {
        _cts?.Cancel();
        _cts?.Dispose();
    }
}
