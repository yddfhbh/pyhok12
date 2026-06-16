import cv2
import mss
import numpy as np


def capture_screen(monitor_index=1):
    """
    현재 화면을 캡처해서 OpenCV BGR 이미지로 반환.
    monitor_index:
      1 = 보통 메인 모니터
      2 = 두 번째 모니터
    """
    with mss.mss() as sct:
        monitors = sct.monitors

        if monitor_index < 1 or monitor_index >= len(monitors):
            monitor_index = 1

        shot = sct.grab(monitors[monitor_index])

    img = np.array(shot)
    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def save_debug_screenshot(img, path="debug_screenshot.png"):
    cv2.imwrite(path, img)