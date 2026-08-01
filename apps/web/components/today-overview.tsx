"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchBootstrap, type Bootstrap } from "@/lib/bootstrap";
import { PageHeader } from "@/components/page-states";

type StatisticsKey = keyof Bootstrap["statistics"];

type MetricDefinition = {
  key: StatisticsKey | "enabled_capabilities";
  label: string;
  note: string;
};

const metricDefinitions: readonly MetricDefinition[] = [
  {
    key: "sources",
    label: "原始来源",
    note: "当前空间保存的来源",
  },
  {
    key: "queued_jobs",
    label: "排队任务",
    note: "等待或正在处理的任务",
  },
  {
    key: "activity_events",
    label: "活动事件",
    note: "当前空间记录的事件",
  },
  {
    key: "enabled_capabilities",
    label: "已开放能力",
    note: "共 6 项规划能力",
  },
];

function LoadingState() {
  return (
    <div aria-label="正在加载首页数据" aria-live="polite" className="metric-band">
      {metricDefinitions.map((metric) => (
        <div className="metric metric-loading" key={metric.key}>
          <span className="metric-label">{metric.label}</span>
          <span className="skeleton-number" />
          <span className="skeleton-line" />
        </div>
      ))}
    </div>
  );
}

function metricValue(data: Bootstrap, key: MetricDefinition["key"]) {
  if (key === "enabled_capabilities") {
    return Object.values(data.capabilities).filter(Boolean).length;
  }

  return data.statistics[key];
}

export function TodayOverview() {
  const query = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
  });

  return (
    <section aria-labelledby="today-title">
      <PageHeader
        eyebrow="FOUNDATION · 知识空间"
        headingId="today-title"
        title="今日学习"
        description="先确认个人知识空间的真实基础状态，再等待后续能力按日程开放。"
      />

      {query.isPending ? <LoadingState /> : null}

      {query.isError ? (
        <div className="state-card error-state" role="alert">
          <p className="state-kicker">CONNECTION · 未取得首页数据</p>
          <h2>无法连接知识工作台 API</h2>
          <p>
            请确认后端正在运行，且 API 地址配置正确。页面没有用演示数字替代真实结果。
          </p>
          <button className="button primary" onClick={() => query.refetch()} type="button">
            重试连接
          </button>
        </div>
      ) : null}

      {query.data ? (
        <>
          <div className="space-identity">
            <span>当前知识空间</span>
            <strong>{query.data.space.name}</strong>
            <small>{query.data.space.slug}</small>
          </div>
          <div className="metric-band" aria-label="知识空间真实指标">
            {metricDefinitions.map((metric) => (
              <article className="metric" key={metric.key}>
                <span className="metric-label">{metric.label}</span>
                <strong>{metricValue(query.data, metric.key)}</strong>
                <small>{metric.note}</small>
              </article>
            ))}
          </div>
          <article className="state-card ready-state">
            <div>
              <p className="state-kicker">D2 · SOURCE IMPORT READY</p>
              <h2>{query.data.space.name}已连接</h2>
            </div>
            <p>
              基础状态为 ready，D2 来源导入已开放。来源、任务、事件和能力数量均来自 bootstrap 接口，包括真实的零值；其余未开放能力不会显示为可用。
            </p>
          </article>
        </>
      ) : null}
    </section>
  );
}
