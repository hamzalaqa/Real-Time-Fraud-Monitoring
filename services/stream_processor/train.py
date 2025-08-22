import pandas as pd, numpy as np, joblib, os
from sklearn.model_selection import train_test_split
from sklearn.metrics import average_precision_score, precision_recall_fscore_support
from xgboost import XGBClassifier
from featurizer import FEATURE_NAMES

CSV = "data/raw/creditcard.csv"
MODEL_OUT = "services/stream_processor/model.pkl"

def load():
    df = pd.read_csv(CSV)
    # Kaggle: columns Time, V1..V28, Amount, Class
    X = df[[*FEATURE_NAMES]].values
    y = df["Class"].values
    return X,y

if __name__=="__main__":
    X,y=load()
    Xtr,Xte,ytr,yte = train_test_split(X,y,stratify=y,test_size=0.2,random_state=42)
    scale_pos = (len(ytr)-ytr.sum())/ytr.sum()
    clf = XGBClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.05,
        subsample=0.9, colsample_bytree=0.9, reg_lambda=1.0,
        objective="binary:logistic", eval_metric="logloss",
        scale_pos_weight=scale_pos
    )
    clf.fit(Xtr,ytr)
    proba = clf.predict_proba(Xte)[:,1]
    ap = average_precision_score(yte, proba)
    print(f"[train] PR-AUC (AP) = {ap:.3f}")
    joblib.dump(clf, MODEL_OUT)
    print(f"[train] saved -> {MODEL_OUT}")
