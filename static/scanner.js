const controls = document.querySelector("[data-scan-mode]");
const camera = document.querySelector("#camera");
const sample = document.querySelector("#sample");
const fileInput = document.querySelector("#scan-image");
const video = document.querySelector("#camera-view");
const statusLine = document.querySelector("#scan-status");
const identifier = document.querySelector("#identifier");
const form = document.querySelector("#identifier-form");
const zxing = window.ZXing;
let cameraReader;
let scanRun = 0;

function newReader() {
  const formats = controls.dataset.scanMode === "qr" ? [zxing.BarcodeFormat.QR_CODE] : [zxing.BarcodeFormat.CODE_128];
  return new zxing.BrowserMultiFormatReader(new Map([[zxing.DecodeHintType.POSSIBLE_FORMATS, formats]]));
}

function stopCamera() {
  scanRun++;
  cameraReader?.reset();
  cameraReader = undefined;
  video.hidden = true;
  camera.textContent = "Start camera";
}

function submitCode(result) {
  stopCamera();
  statusLine.textContent = "Code read. Looking up the patient…";
  identifier.value = result.getText();
  form.requestSubmit();
}

camera.addEventListener("click", async () => {
  if (cameraReader) {
    stopCamera();
    statusLine.textContent = "Camera stopped.";
    return;
  }
  if (!zxing || !navigator.mediaDevices?.getUserMedia) {
    statusLine.textContent = "Camera scanning is unavailable. Use an image or enter the code.";
    return;
  }
  const reader = cameraReader = newReader();
  const run = ++scanRun;
  camera.textContent = "Stop camera";
  video.hidden = false;
  statusLine.textContent = "Allow camera access, then hold the code in view.";
  try {
    await reader.decodeFromVideoDevice(null, video, result => {
      if (result && scanRun === run) submitCode(result);
    });
    if (scanRun !== run) reader.reset();
  } catch {
    if (scanRun !== run) return;
    stopCamera();
    statusLine.textContent = "Could not open the camera. Use an image or enter the code.";
  }
});

async function scanImage(url) {
  stopCamera();
  const run = scanRun;
  statusLine.textContent = "Reading the image…";
  try {
    const result = await newReader().decodeFromImageUrl(url);
    if (scanRun === run) submitCode(result);
  } catch {
    if (scanRun === run) statusLine.textContent = "Could not read this code. Try a clearer image or enter it.";
  }
}

sample.addEventListener("click", () => scanImage(sample.dataset.image));
fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  const url = URL.createObjectURL(file);
  try {
    await scanImage(url);
  } finally {
    URL.revokeObjectURL(url);
    fileInput.value = "";
  }
});
window.addEventListener("pagehide", stopCamera);
