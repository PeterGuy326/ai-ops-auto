"""招聘平台适配器（Applier）。

与 publishers/ 的关键区别：PublisherBase 是「一锤子推内容」，
ApplierBase 多了反向动作——search_jobs（采集）和 poll_replies（回复轮询）。
"""
