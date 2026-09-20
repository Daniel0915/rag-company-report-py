class DartApiError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"DART API 오류 [{status}]: {message}")
        self.status = status
