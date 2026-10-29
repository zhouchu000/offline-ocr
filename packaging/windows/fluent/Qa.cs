// Compiled only into separately named acceptance builds; no QA switches ship.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Interop;

namespace OfflineWord.Setup {
    internal static partial class ProtocolTests {
        public static int Run(Bundle bundle,string output) {
            Directory.CreateDirectory(output);
            try {
                string prepared=bundle.Prepare(new Progress<ProgressState>(),System.Threading.CancellationToken.None).GetAwaiter().GetResult();
                if(!File.Exists(prepared))throw new Exception("No prepared backend");
                File.WriteAllText(Path.Combine(output,"protocol.json"),"{\"prepared\":true,\"executed\":false}");
                return 0;
            }catch(SetupFailure error) {
                File.WriteAllText(Path.Combine(output,"protocol.json"),"{\"prepared\":false,\"code\":\""+error.Code+"\"}");
                return 1;
            }finally {bundle.Dispose();}
        }
    }
    public sealed partial class SetupWindow {
        [StructLayout(LayoutKind.Sequential)] struct QaRect {public int Left,Top,Right,Bottom;}
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr window,out QaRect rectangle);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
        void CaptureComposed(string folder,string name) {
            IntPtr handle=new WindowInteropHelper(this).Handle;
            if(GetForegroundWindow()!=handle)throw new Exception("Owned installer is not foreground; no screen capture performed");
            QaRect rectangle;if(!GetWindowRect(handle,out rectangle))throw new Exception("Owned bounds unavailable");
            using(var bitmap=new System.Drawing.Bitmap(rectangle.Right-rectangle.Left,rectangle.Bottom-rectangle.Top)) {
                using(var graphics=System.Drawing.Graphics.FromImage(bitmap))graphics.CopyFromScreen(rectangle.Left,rectangle.Top,0,0,bitmap.Size);
                bitmap.Save(Path.Combine(folder,name+".png"),System.Drawing.Imaging.ImageFormat.Png);
            }
        }
        public void StartInstallQa(string output) {
            qaPermitInstall=true;
            Loaded+=async(s,e)=> {
                try {
                    Directory.CreateDirectory(output);
                    var names=new List<string>();
                    GlyphTypeface glyph;
                    if(!new Typeface(FontFamily,FontStyles.Normal,FontWeights.Normal,FontStretches.Normal).TryGetGlyphTypeface(out glyph)
                       ||!glyph.FontUri.ToString().ToLowerInvariant().Contains("misans"))throw new Exception("Bundled font did not load");
                    var pending=RunOperation();
                    Button("SecondaryButton").RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Button.ClickEvent));
                    await pending;
                    if(state!="ready"||Directory.Exists(root))throw new Exception("Cancellation changed the installation");
                    Capture(output,"cancelled",names);
                    await RunOperation();
                    if(state!="success")throw new Exception("Install failed: "+Label("Description").Text);
                    Capture(output,"installed",names);
                    using(Native.AcquireInstallLock(root)) {
                        await RunOperation();
                        if(state!="error"||!Label("Description").Text.Contains("关闭离线 OCR"))throw new Exception("Busy installation was not refused");
                        Capture(output,"busy-refused",names);
                    }
                    await RunOperation();
                    if(state!="success")throw new Exception("Retry failed: "+Label("Description").Text);
                    Capture(output,"retried",names);
                    File.WriteAllText(Path.Combine(output,"report.json"),"{\"pass\":true,\"cancel_before_write\":true,\"native_install\":true,\"lock_refusal\":true,\"retry\":true}",Encoding.UTF8);
                    Close();
                }catch(Exception error){busy=writing=false;File.WriteAllText(Path.Combine(output,"error.txt"),error.ToString());Application.Current.Shutdown(1);}
            };
        }
        public void StartQa(string output) {
            Loaded+=async(s,e)=> {
                try {
                    Directory.CreateDirectory(output);
                    var names=new List<string>();
                    if(lowPerformance||view.FindName("ThemeButton")!=null)throw new Exception("Installer must default to dark without a theme action");
                    ShowSettings(true);await Task.Delay(150);Capture(output,"dark-settings",names);
                    Control<ToggleButton>("LowPerformanceToggle").IsChecked=true;
                    if(!lowPerformance||GlassActive||MotionAllowed||Control<Grid>("LightEffects").Visibility!=Visibility.Collapsed
                       ||((SolidColorBrush)Control<Border>("MaterialRoot").Background).Color.A!=255)throw new Exception("Low performance mode kept visual effects enabled");
                    AnimatePage();ShowProgress(new ProgressState("installing",-1));
                    if(Control<Grid>("Page").HasAnimatedProperties||Control<Grid>("Page").RenderTransform.HasAnimatedProperties||Control<ProgressBar>("Progress").IsIndeterminate)
                        throw new Exception("Low performance mode kept animations enabled");
                    await Task.Delay(150);Capture(output,"low-performance-settings",names);
                    Control<ToggleButton>("LowPerformanceToggle").IsChecked=false;
                    if(lowPerformance)throw new Exception("Default appearance was not restored");
                    ShowSettings(false);Ready();
                    foreach(bool shade in new[]{false,true}) {
                        lowPerformance=!shade;ApplyTheme();Ready();
                        await Task.Delay(400);
                        Capture(output,(shade?"dark":"light")+"-ready",names);
                        ToggleNotes();await Task.Delay(320);Capture(output,(shade?"dark":"light")+"-notes",names);ToggleNotes();
                        busy=writing=true;Control<Border>("InfoPanel").Visibility=Visibility.Collapsed;
                        Control<StackPanel>("ProgressPanel").Visibility=Visibility.Visible;
                        Label("Heading").Text="正在安装离线 OCR。";Label("Description").Text="稍作等待，一切都在本机完成。";
                        SetButtons(false);ShowProgress(new ProgressState("verifying",.63));await Task.Delay(350);
                        Capture(output,(shade?"dark":"light")+"-progress",names);
                        var cancel=new System.ComponentModel.CancelEventArgs();OnClosing(this,cancel);
                        if(!cancel.Cancel)throw new Exception("Writing may not be closed");
                        ShowSettings(true);
                        Control<ToggleButton>("LowPerformanceToggle").IsChecked=true;
                        if(Control<ProgressBar>("Progress").IsIndeterminate)throw new Exception("Changing modes while busy retained animation");
                        busy=writing=false;ShowSuccess();
                        if(Button("PrimaryButton").IsEnabled||Button("SecondaryButton").IsEnabled)throw new Exception("Completion enabled actions behind settings");
                        ShowSettings(false);lowPerformance=!shade;ApplyTheme();
                        busy=writing=false;state="success";ShowSuccess();await Task.Delay(350);Capture(output,(shade?"dark":"light")+"-success",names);
                        ShowError("application_running");await Task.Delay(350);Capture(output,(shade?"dark":"light")+"-error",names);
                    }
                    Width=660;Height=460;lowPerformance=true;ApplyTheme();Ready();await Task.Delay(400);Capture(output,"small-light",names);
                    ShowSettings(true);UpdateLayout();Capture(output,"small-settings",names);
                    foreach(string name in new[]{"PrimaryButton","SecondaryButton","CloseButton","SettingsButton","SettingsPanel","LowPerformanceToggle"}) {
                        var control=Control<FrameworkElement>(name);
                        var bounds=control.TransformToAncestor(view).TransformBounds(new Rect(control.RenderSize));
                        if(bounds.Left<0||bounds.Top<0||bounds.Right>view.ActualWidth+1||bounds.Bottom>view.ActualHeight+1)throw new Exception("Clipped action: "+name);
                    }
                    ShowSettings(false);Width=760;Height=540;Ready();
                    var backdrop=new Window {WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.NoResize,ShowInTaskbar=false,
                        Left=Left-20,Top=Top-20,Width=Width+40,Height=Height+40,
                        Background=new LinearGradientBrush(Color.FromRgb(92,147,205),Color.FromRgb(199,149,181),25)};
                    backdrop.Show();Owner=backdrop;Activate();SetForegroundWindow(new WindowInteropHelper(this).Handle);
                    lowPerformance=true;ApplyTheme();await Task.Delay(450);CaptureComposed(output,"composed-light");
                    lowPerformance=false;ApplyTheme();await Task.Delay(450);CaptureComposed(output,"composed-dark");
                    ApplyTheme();AnimatePage();await Task.Delay(75);Capture(output,"motion-middle",names);
                    await Task.Delay(350);Capture(output,"motion-finished",names);
                    Owner=null;backdrop.Close();
                    File.WriteAllText(Path.Combine(output,"report.json"),"{\"pass\":true,\"native_wpf\":true,\"default_dark\":true,\"settings_toggle\":true,\"low_performance_disables_effects\":true,\"low_performance_disables_motion\":true,\"owned_composition_captured\":true,\"glass_active\":"+GlassActive.ToString().ToLowerInvariant()+",\"states\":"+names.Count+",\"font\":\""+FontFamily.Source+"\"}",Encoding.UTF8);
                    Close();
                }catch(Exception error){File.WriteAllText(Path.Combine(output,"error.txt"),error.ToString());Application.Current.Shutdown(1);}
            };
        }
        void Capture(string folder,string name,List<string> names) {
            UpdateLayout();
            var dpi=VisualTreeHelper.GetDpi(view);
            var target=new RenderTargetBitmap((int)Math.Ceiling(view.ActualWidth*dpi.DpiScaleX),(int)Math.Ceiling(view.ActualHeight*dpi.DpiScaleY),dpi.PixelsPerInchX,dpi.PixelsPerInchY,PixelFormats.Pbgra32);
            target.Render(view);
            var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(target));
            using(var file=File.Create(Path.Combine(folder,name+".png")))encoder.Save(file);
            names.Add(name);
        }
    }
}
