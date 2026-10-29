# 本地模型许可

完整开源包包含 models.lock.json 固定的 10 个 PaddlePaddle 官方模型，权重保持原样。各模型的官方模型卡均标明 Apache-2.0，来源、模型卡版本及核验日期见 model-provenance.json。原始 Apache-2.0 许可全文见 PaddleOCR-3.7.0-LICENSE.txt 和 PaddleX-3.7.0-LICENSE.txt；模型版权属于上游贡献者。

模型归档来源和 SHA-256 记录于 models.lock.json；完整发布包的 models/manifest.json 记录每个模型文件的 SHA-256。公开源码导出时校验原目录与复制结果，拒绝缺失、变更、额外文件或链接。

模型不需要在运行时下载，亦不因本程序封装而改换许可。
