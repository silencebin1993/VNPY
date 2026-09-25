"""
模型注册表：每种模型一个小适配器（训练 / 预测 / 因子重要性 / 存取），下拉框里选。
- 树模型（LightGBM / XGBoost / CatBoost / HistGBDT）用训练期最后 10% 的交易日做验证集提前停止（按时间切，不随机）；
  数值预测的提前停止指标是"按天的相关系数"（和选股排序一致），分类用 AUC；
- 随机森林、神经网络等慢的模型，每期最多抽样一定行数训练（页面会写明）；
- "等权打分"不训练：每个因子按训练期的方向取正负后等权相加，作为对照——复杂模型连它都比不过就没有意义。
只有 LightGBM 能给出"每只股票为什么得这个分"的拆解（pred_contrib），其他模型只有整体的因子重要性。
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

PIP_HINT: str = r".venv\Scripts\pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "

# 每个预设：学习率、最多轮数、提前停止、叶子数/深度
BOOST: dict[str, dict[str, float]] = {
    "fast": {"lr": 0.1, "rounds": 300, "early": 30, "leaves": 31, "depth": 5},
    "standard": {"lr": 0.05, "rounds": 1000, "early": 60, "leaves": 31, "depth": 6},
    "fine": {"lr": 0.03, "rounds": 2000, "early": 100, "leaves": 63, "depth": 7},
}


@dataclass
class Fitted:
    obj: Any
    best_iter: int | None = None
    extra: dict = field(default_factory=dict)


def day_ic(pred: np.ndarray, y: np.ndarray, gid: np.ndarray) -> float:
    """按天的 Pearson 相关系数的平均（gid = 每行属于第几天，0..n-1）"""
    pred = np.asarray(pred, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = np.bincount(gid).astype(np.float64)
    safe_n = np.where(n > 0, n, 1)
    dp = pred - (np.bincount(gid, pred) / safe_n)[gid]
    dy = y - (np.bincount(gid, y) / safe_n)[gid]
    cov = np.bincount(gid, dp * dy)
    vp = np.bincount(gid, dp * dp)
    vy = np.bincount(gid, dy * dy)
    ok = (n >= 5) & (vp > 1e-18) & (vy > 1e-18)
    return float(np.mean(cov[ok] / np.sqrt(vp[ok] * vy[ok]))) if ok.any() else 0.0


class Adapter:
    def fit(self, X: np.ndarray, y: np.ndarray, task: str, preset: str, *, Xv: np.ndarray | None = None,
            yv: np.ndarray | None = None, gv: np.ndarray | None = None, g: np.ndarray | None = None,
            rounds: int | None = None, threads: int = 0, seed: int = 7, names: list[str] | None = None) -> Fitted:
        raise NotImplementedError

    def predict(self, m: Fitted, X: np.ndarray, task: str) -> np.ndarray:
        raise NotImplementedError

    def importance(self, m: Fitted, names: list[str]) -> dict[str, float] | None:
        return None

    def contrib(self, m: Fitted, X: np.ndarray) -> np.ndarray | None:
        return None

    def save(self, m: Fitted, path: Path) -> None:
        import joblib
        joblib.dump(m, path.with_suffix(".joblib"))

    def load(self, path: Path) -> Fitted:
        import joblib
        return joblib.load(path.with_suffix(".joblib"))


def _norm_imp(names: list[str], values: np.ndarray) -> dict[str, float]:
    v = np.nan_to_num(np.abs(np.asarray(values, dtype=np.float64)))
    total = float(v.sum()) or 1.0
    return {n: float(x / total) for n, x in zip(names, v, strict=True)}


# ---------------------------------------------------------------- LightGBM

class LightGBMAdapter(Adapter):
    def fit(self, X, y, task, preset, *, Xv=None, yv=None, gv=None, g=None, rounds=None, threads=0, seed=7, names=None) -> Fitted:
        import lightgbm as lgb
        b = BOOST[preset]
        params: dict = {"objective": "binary" if task == "cls" else "regression", "learning_rate": b["lr"],
                        "num_leaves": int(b["leaves"]), "min_data_in_leaf": 200, "feature_fraction": 0.8, "bagging_fraction": 0.8,
                        "bagging_freq": 1, "lambda_l2": 1.0 if task == "cls" else 10.0, "num_threads": threads, "verbose": -1,
                        "seed": seed, "metric": "auc" if task == "cls" else "None"}
        dtrain = lgb.Dataset(X, y, feature_name=[f"f{i}" for i in range(X.shape[1])], free_raw_data=True)
        if rounds is not None or Xv is None or len(Xv) == 0:
            n = int(rounds or b["rounds"] // 3)
            return Fitted(lgb.train(params, dtrain, num_boost_round=max(n, 10)), max(n, 10))
        dvalid = lgb.Dataset(Xv, yv, reference=dtrain)
        feval = None
        if task != "cls" and gv is not None:
            feval = lambda preds, data: ("day_ic", day_ic(preds, yv, gv), True)             # noqa: E731
        bst = lgb.train(params, dtrain, num_boost_round=int(b["rounds"]), valid_sets=[dvalid], feval=feval,
                        callbacks=[lgb.early_stopping(int(b["early"]), verbose=False)])
        best = int(bst.best_iteration or bst.current_iteration())
        return Fitted(bst, max(best, 1))

    def predict(self, m, X, task):
        return np.asarray(m.obj.predict(X, num_iteration=m.best_iter), dtype=np.float64)

    def importance(self, m, names):
        return _norm_imp(names, m.obj.feature_importance(importance_type="gain"))

    def contrib(self, m, X):
        return np.asarray(m.obj.predict(X, num_iteration=m.best_iter, pred_contrib=True))[:, :-1]

    def save(self, m, path):
        m.obj.save_model(str(path.with_suffix(".txt")), num_iteration=m.best_iter)
        path.with_suffix(".meta.json").write_text(json.dumps({"best_iter": m.best_iter}), encoding="utf-8")

    def load(self, path):
        import lightgbm as lgb
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        return Fitted(lgb.Booster(model_file=str(path.with_suffix(".txt"))), meta.get("best_iter"))


# ---------------------------------------------------------------- XGBoost

class XGBoostAdapter(Adapter):
    def fit(self, X, y, task, preset, *, Xv=None, yv=None, gv=None, g=None, rounds=None, threads=0, seed=7, names=None) -> Fitted:
        import xgboost as xgb
        b = BOOST[preset]
        params: dict = {"objective": "binary:logistic" if task == "cls" else "reg:squarederror", "eta": b["lr"],
                        "max_depth": int(b["depth"]), "min_child_weight": 50, "subsample": 0.8, "colsample_bytree": 0.8,
                        "lambda": 10.0, "tree_method": "hist", "nthread": threads or None, "seed": seed, "verbosity": 0}
        if task == "cls":
            params["eval_metric"] = "auc"
        dtrain = xgb.DMatrix(X, label=y, missing=np.nan)
        if rounds is not None or Xv is None or len(Xv) == 0:
            n = int(rounds or b["rounds"] // 3)
            return Fitted(xgb.train(params, dtrain, num_boost_round=max(n, 10)), max(n, 10))
        dvalid = xgb.DMatrix(Xv, label=yv, missing=np.nan)
        kw: dict = {}
        if task != "cls" and gv is not None:
            params["disable_default_eval_metric"] = 1
            kw = {"custom_metric": lambda preds, d: ("day_ic", day_ic(preds, yv, gv)), "maximize": True}
        bst = xgb.train(params, dtrain, num_boost_round=int(b["rounds"]), evals=[(dvalid, "valid")],
                        early_stopping_rounds=int(b["early"]), verbose_eval=False, **kw)
        return Fitted(bst, int(bst.best_iteration) + 1)

    def predict(self, m, X, task):
        import xgboost as xgb
        return np.asarray(m.obj.predict(xgb.DMatrix(X, missing=np.nan), iteration_range=(0, int(m.best_iter or 0))), dtype=np.float64)

    def importance(self, m, names):
        sc = m.obj.get_score(importance_type="gain")
        return _norm_imp(names, np.array([sc.get(f"f{i}", 0.0) for i in range(len(names))]))

    def save(self, m, path):
        m.obj.save_model(str(path.with_suffix(".json")))
        path.with_suffix(".meta.json").write_text(json.dumps({"best_iter": m.best_iter}), encoding="utf-8")

    def load(self, path):
        import xgboost as xgb
        bst = xgb.Booster()
        bst.load_model(str(path.with_suffix(".json")))
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        return Fitted(bst, meta.get("best_iter"))


# ---------------------------------------------------------------- CatBoost

class CatBoostAdapter(Adapter):
    def fit(self, X, y, task, preset, *, Xv=None, yv=None, gv=None, g=None, rounds=None, threads=0, seed=7, names=None) -> Fitted:
        from catboost import CatBoostClassifier, CatBoostRegressor
        b = BOOST[preset]
        cls = CatBoostClassifier if task == "cls" else CatBoostRegressor
        kw: dict = {"learning_rate": b["lr"], "depth": int(b["depth"]), "random_seed": seed, "verbose": False,
                    "allow_writing_files": False, "thread_count": threads or -1, "l2_leaf_reg": 10.0}
        if task == "cls":
            kw["eval_metric"] = "AUC"
        if rounds is not None or Xv is None or len(Xv) == 0:
            n = int(rounds or b["rounds"] // 3)
            m = cls(iterations=max(n, 10), **kw)
            m.fit(X, y)
            return Fitted(m, max(n, 10))
        m = cls(iterations=int(b["rounds"]), od_type="Iter", od_wait=int(b["early"]), **kw)
        m.fit(X, y, eval_set=(Xv, yv), use_best_model=True)
        return Fitted(m, int(m.get_best_iteration() or 0) + 1)

    def predict(self, m, X, task):
        if task == "cls":
            return np.asarray(m.obj.predict_proba(X)[:, 1], dtype=np.float64)
        return np.asarray(m.obj.predict(X), dtype=np.float64)

    def importance(self, m, names):
        return _norm_imp(names, m.obj.get_feature_importance())

    def save(self, m, path):
        m.obj.save_model(str(path.with_suffix(".cbm")))
        path.with_suffix(".meta.json").write_text(json.dumps({"best_iter": m.best_iter, "cls": type(m.obj).__name__}), encoding="utf-8")

    def load(self, path):
        from catboost import CatBoostClassifier, CatBoostRegressor
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        obj = (CatBoostClassifier if meta.get("cls") == "CatBoostClassifier" else CatBoostRegressor)()
        obj.load_model(str(path.with_suffix(".cbm")))
        return Fitted(obj, meta.get("best_iter"))


# ---------------------------------------------------------------- scikit-learn

class SklearnAdapter(Adapter):
    """make(task, preset, threads, seed) -> 估计器；imp: "feature_importances_" / "coef_" / None"""

    def __init__(self, make, imp: str | None = None, val: bool = False) -> None:
        self.make = make
        self.imp = imp
        self.val = val                      # 能不能传时间顺序的验证集（HistGBDT）

    def fit(self, X, y, task, preset, *, Xv=None, yv=None, gv=None, g=None, rounds=None, threads=0, seed=7, names=None) -> Fitted:
        est = self.make(task, preset, threads, seed, rounds)
        yy = y.astype(np.int32) if task == "cls" else y
        if self.val and rounds is None and Xv is not None and len(Xv):
            est.fit(X, yy, X_val=Xv, y_val=yv.astype(np.int32) if task == "cls" else yv)
        else:
            est.fit(X, yy)
        n_iter = getattr(est, "n_iter_", None)                       # 逻辑回归是数组，其余是整数
        best = int(np.max(np.atleast_1d(n_iter))) if n_iter is not None else None
        return Fitted(est, best or None)

    def predict(self, m, X, task):
        if task == "cls":
            return np.asarray(m.obj.predict_proba(X)[:, 1], dtype=np.float64)
        return np.asarray(m.obj.predict(X), dtype=np.float64)

    def importance(self, m, names):
        if self.imp == "feature_importances_" and hasattr(m.obj, "feature_importances_"):
            return _norm_imp(names, m.obj.feature_importances_)
        if self.imp == "coef_" and hasattr(m.obj, "coef_"):
            return _norm_imp(names, np.ravel(m.obj.coef_))
        return None


def _rf(kind: str):
    def make(task, preset, threads, seed, rounds):
        from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor, RandomForestClassifier, RandomForestRegressor
        n = {"fast": 60, "standard": 150, "fine": 300}[preset]
        depth = {"fast": 8, "standard": 10, "fine": 12}[preset]
        cls = {("rf", "cls"): RandomForestClassifier, ("rf", "reg"): RandomForestRegressor,
               ("et", "cls"): ExtraTreesClassifier, ("et", "reg"): ExtraTreesRegressor}[(kind, task)]
        return cls(n_estimators=n, max_depth=depth, min_samples_leaf=200, max_features=0.3, bootstrap=True, max_samples=0.5,
                   n_jobs=threads or -1, random_state=seed)
    return make


def _hgb(task, preset, threads, seed, rounds):
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    b = BOOST[preset]
    cls = HistGradientBoostingClassifier if task == "cls" else HistGradientBoostingRegressor
    if rounds is not None:
        return cls(max_iter=max(int(rounds), 10), learning_rate=b["lr"], max_leaf_nodes=int(b["leaves"]), min_samples_leaf=200,
                   l2_regularization=10.0, early_stopping=False, random_state=seed)
    return cls(max_iter=int(b["rounds"]), learning_rate=b["lr"], max_leaf_nodes=int(b["leaves"]), min_samples_leaf=200,
               l2_regularization=10.0, early_stopping=True, n_iter_no_change=int(b["early"]), random_state=seed)


def _linear(kind: str):
    def make(task, preset, threads, seed, rounds):
        from sklearn.linear_model import ElasticNet, Lasso, LogisticRegression, Ridge
        strength = {"fast": 1.0, "standard": 1.0, "fine": 0.5}[preset]
        if task == "cls":
            pen = {"ridge": "l2", "lasso": "l1", "enet": "elasticnet", "logit": "l2"}[kind]
            kw: dict = {"l1_ratio": 0.5} if pen == "elasticnet" else {}
            return LogisticRegression(penalty=pen, C=0.1 / strength, solver="saga", max_iter=200, random_state=seed, **kw)
        if kind == "ridge":
            return Ridge(alpha=100.0 * strength, random_state=seed)
        if kind == "lasso":
            return Lasso(alpha=1e-4 * strength, max_iter=2000, random_state=seed)
        return ElasticNet(alpha=2e-4 * strength, l1_ratio=0.5, max_iter=2000, random_state=seed)
    return make


def _mlp(task, preset, threads, seed, rounds):
    from sklearn.neural_network import MLPClassifier, MLPRegressor
    cls = MLPClassifier if task == "cls" else MLPRegressor
    size = {"fast": (32,), "standard": (64, 32), "fine": (128, 64)}[preset]
    it = {"fast": 20, "standard": 50, "fine": 100}[preset]
    return cls(hidden_layer_sizes=size, alpha=1e-3, batch_size=4096, learning_rate_init=1e-3, max_iter=int(rounds or it),
               early_stopping=rounds is None, validation_fraction=0.1, n_iter_no_change=5, random_state=seed)


# ---------------------------------------------------------------- 等权打分（对照）

class EqualWeightAdapter(Adapter):
    def fit(self, X, y, task, preset, *, Xv=None, yv=None, gv=None, g=None, rounds=None, threads=0, seed=7, names=None) -> Fitted:
        gid = g if g is not None else np.zeros(len(y), dtype=np.int64)
        X0 = np.nan_to_num(X)
        ics = np.array([day_ic(X0[:, j], y, gid) for j in range(X.shape[1])])
        w = np.sign(ics) / max(X.shape[1], 1)
        return Fitted({"w": w, "ic": ics}, None)

    def predict(self, m, X, task):
        return np.nan_to_num(X) @ m.obj["w"]

    def importance(self, m, names):
        return _norm_imp(names, m.obj["ic"])


# ---------------------------------------------------------------- 注册表

@dataclass
class ModelSpec:
    key: str
    label: str
    desc: str
    family: str
    adapter: Adapter
    tasks: tuple[str, ...] = ("reg", "cls")
    package: str | None = None
    pip: str = ""
    nan_ok: bool = False                 # 能直接处理空值（不能的，训练前把空值填成 0 = 当天的中间位置）
    speed: float = 1.0                   # 相对 LightGBM 的耗时（估算用）
    rows: dict[str, int | None] = field(default_factory=lambda: {"fast": None, "standard": None, "fine": None})
    recommended: bool = False
    explain: bool = False                # 能不能拆解单只股票的得分

    def available(self) -> tuple[bool, str]:
        if self.package and importlib.util.find_spec(self.package) is None:
            return False, f"还没有安装 {self.package}：在程序目录的命令行运行 {PIP_HINT}{self.pip or self.package}"
        return True, ""

    def max_rows(self, preset: str) -> int | None:
        return self.rows.get(preset)


def _rows(fast: int, standard: int, fine: int) -> dict[str, int | None]:
    return {"fast": fast, "standard": standard, "fine": fine}


MODELS: dict[str, ModelSpec] = {m.key: m for m in [
    ModelSpec("lightgbm", "LightGBM（推荐）", "梯度提升树，又快又准，量化圈最常用；能拆解每只股票的得分原因", "gbdt",
              LightGBMAdapter(), package="lightgbm", nan_ok=True, speed=1.0, recommended=True, explain=True),
    ModelSpec("xgboost", "XGBoost", "另一种梯度提升树，效果和 LightGBM 接近，稍慢", "gbdt", XGBoostAdapter(),
              package="xgboost", nan_ok=True, speed=1.6),
    ModelSpec("catboost", "CatBoost", "梯度提升树，对参数不敏感、不容易过拟合，但训练慢", "gbdt", CatBoostAdapter(),
              package="catboost", nan_ok=True, speed=3.0, rows=_rows(1_000_000, 2_000_000, 4_000_000)),
    ModelSpec("hist_gbdt", "HistGBDT（sklearn）", "scikit-learn 自带的梯度提升树，不用额外安装", "gbdt",
              SklearnAdapter(_hgb, None, val=True), package="sklearn", nan_ok=True, speed=1.3),
    ModelSpec("random_forest", "随机森林", "很多棵树投票，稳但偏保守；训练慢，每期最多抽样一部分行", "forest",
              SklearnAdapter(_rf("rf"), "feature_importances_"), package="sklearn", nan_ok=True, speed=4.0,
              rows=_rows(300_000, 600_000, 1_200_000)),
    ModelSpec("extra_trees", "极端随机树", "随机森林的变种，更快、更随机", "forest",
              SklearnAdapter(_rf("et"), "feature_importances_"), package="sklearn", nan_ok=True, speed=3.0,
              rows=_rows(300_000, 600_000, 1_200_000)),
    ModelSpec("ridge", "岭回归（线性）", "最简单的线性模型：每个因子一个权重。可解释、不容易过拟合", "linear",
              SklearnAdapter(_linear("ridge"), "coef_"), package="sklearn", speed=0.1),
    ModelSpec("lasso", "Lasso（线性、会挑因子）", "线性模型，会把没用的因子权重压成 0", "linear",
              SklearnAdapter(_linear("lasso"), "coef_"), package="sklearn", speed=0.3),
    ModelSpec("elasticnet", "ElasticNet（线性）", "岭回归和 Lasso 的折中", "linear",
              SklearnAdapter(_linear("enet"), "coef_"), package="sklearn", speed=0.3),
    ModelSpec("mlp", "神经网络 MLP（sklearn）", "两层小神经网络。股票数据噪声大，神经网络通常不比树模型好，训练也慢", "nn",
              SklearnAdapter(_mlp, None), package="sklearn", speed=3.0, rows=_rows(500_000, 1_000_000, 2_000_000)),
    ModelSpec("logistic", "逻辑回归（分类）", "线性分类模型，只能配“赚超过 5%”这类分类目标", "linear",
              SklearnAdapter(_linear("logit"), "coef_"), tasks=("cls",), package="sklearn", speed=0.3,
              rows=_rows(1_000_000, 2_000_000, 4_000_000)),
    ModelSpec("equal_weight", "等权打分（不训练，对照）", "每个因子按训练期的方向取正负后等权相加。复杂模型连它都比不过就没有意义", "baseline",
              EqualWeightAdapter(), speed=0.02),
]}


def get(key: str) -> ModelSpec:
    if key not in MODELS:
        raise ValueError(f"不认识的模型「{key}」")
    return MODELS[key]


def listing() -> list[dict]:
    out: list[dict] = []
    for m in MODELS.values():
        ok, why = m.available()
        out.append({"key": m.key, "label": m.label, "desc": m.desc, "family": m.family, "tasks": list(m.tasks),
                    "available": ok, "reason": why, "recommended": m.recommended, "explain": m.explain,
                    "sampled": {k: v for k, v in m.rows.items() if v}})
    return out
