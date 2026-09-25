"""测试环境：旧的每日流水线测试不跑投资助手（它有自己的测试），避免在临时数据上做重计算"""
import os

os.environ.setdefault("QUANT_WEB_NO_ASSISTANT", "1")
