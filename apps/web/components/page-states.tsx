type PageHeaderProps = {
  eyebrow: string;
  title: string;
  description: string;
  headingId?: string;
};

export function PageHeader({ eyebrow, title, description, headingId = "page-title" }: PageHeaderProps) {
  return (
    <header className="page-head">
      <p className="eyebrow">{eyebrow}</p>
      <h1 id={headingId}>{title}</h1>
      <p className="page-subtitle">{description}</p>
    </header>
  );
}

type ScheduledEmptyStateProps = PageHeaderProps & {
  day: string;
  capability: string;
  detail: string;
};

export function ScheduledEmptyState({
  eyebrow,
  title,
  description,
  day,
  capability,
  detail,
}: ScheduledEmptyStateProps) {
  return (
    <section aria-labelledby="page-title">
      <PageHeader eyebrow={eyebrow} title={title} description={description} />
      <article className="schedule-card">
        <div className="schedule-index" aria-hidden="true">
          <span>DELIVERY</span>
          <strong>{day}</strong>
        </div>
        <div className="schedule-copy">
          <p className="status-line"><span />按日程待实现</p>
          <h2>{capability}</h2>
          <p>{detail}</p>
          <div className="honesty-note">
            当前页面不提供演示数据或不可用控件。对应后端契约和真实流程完成后，能力将在这里接入。
          </div>
        </div>
      </article>
    </section>
  );
}
