import type {
  ArtifactContent,
  DerivedArtifact,
  FactReviewStatus,
} from "@/types/artifacts";


type Props = {
  artifact: DerivedArtifact;
  content: ArtifactContent;
  disabled: boolean;
  onChange: (content: ArtifactContent) => void;
  onCancel: () => void;
  onSave: () => void;
};


const FACT_STATUSES: FactReviewStatus[] = [
  "supported",
  "partially_supported",
  "contradicted",
  "unsupported",
  "ambiguous",
];


function updateAt<T>(items: T[], index: number, patch: Partial<T>): T[] {
  return items.map((item, itemIndex) => (
    itemIndex === index ? { ...item, ...patch } : item
  ));
}


export function ArtifactReviewEditor({
  artifact,
  content,
  disabled,
  onChange,
  onCancel,
  onSave,
}: Props) {
  const sections = content.sections ?? [];
  const points = content.key_points ?? [];
  const chapters = content.chapters ?? [];
  const reviews = content.reviews ?? [];

  return (
    <section className="artifactReviewEditor">
      <header>
        <div>
          <span className="label">Human review</span>
          <h3>创建 Artifact v{artifact.artifact_version + 1}</h3>
        </div>
        <small>只修改内容；证据引用和时间范围保持锁定。</small>
      </header>

      {artifact.artifact_kind === "clean_script" ? (
        <label>
          <span>标题</span>
          <input
            value={content.title ?? ""}
            onChange={(event) => onChange({
              ...content,
              title: event.target.value,
            })}
            disabled={disabled}
          />
        </label>
      ) : null}

      {sections.map((section, index) => (
        <article key={`${artifact.artifact_id}-edit-${index}`}>
          <div className="artifactEditTrace">
            <span>#{index + 1}</span>
            <code>{section.source_item_ids.join(", ")}</code>
          </div>
          {section.source_text ? <blockquote>{section.source_text}</blockquote> : null}
          <label>
            <span>
              {artifact.artifact_kind === "refined_translation"
                ? "最终译文"
                : "台本文本"}
            </span>
            <textarea
              value={section.translated_text ?? section.clean_text ?? ""}
              onChange={(event) => onChange({
                ...content,
                sections: updateAt(sections, index, (
                  artifact.artifact_kind === "refined_translation"
                    ? { translated_text: event.target.value }
                    : { clean_text: event.target.value }
                )),
              })}
              rows={4}
              disabled={disabled}
            />
          </label>
          <label>
            <span>备注（每行一条）</span>
            <textarea
              value={section.notes.join("\n")}
              onChange={(event) => onChange({
                ...content,
                sections: updateAt(sections, index, {
                  notes: event.target.value.split(/\r?\n/).filter(Boolean),
                }),
              })}
              rows={2}
              disabled={disabled}
            />
          </label>
        </article>
      ))}

      {artifact.artifact_kind === "summary" ? (
        <>
          <label>
            <span>摘要</span>
            <textarea
              value={content.brief ?? ""}
              onChange={(event) => onChange({
                ...content,
                brief: event.target.value,
              })}
              rows={5}
              disabled={disabled}
            />
          </label>
          {points.map((point, index) => (
            <label key={`${artifact.artifact_id}-point-edit-${index}`}>
              <span>要点 {index + 1}</span>
              <textarea
                value={point.text}
                onChange={(event) => onChange({
                  ...content,
                  key_points: updateAt(points, index, {
                    text: event.target.value,
                  }),
                })}
                rows={3}
                disabled={disabled}
              />
            </label>
          ))}
        </>
      ) : null}

      {artifact.artifact_kind === "chapter_outline"
        ? chapters.map((chapter, index) => (
            <article key={`${artifact.artifact_id}-chapter-edit-${index}`}>
              <label>
                <span>章节 {index + 1} 标题</span>
                <input
                  value={chapter.title}
                  onChange={(event) => onChange({
                    ...content,
                    chapters: updateAt(chapters, index, {
                      title: event.target.value,
                    }),
                  })}
                  disabled={disabled}
                />
              </label>
              <label>
                <span>章节摘要</span>
                <textarea
                  value={chapter.summary}
                  onChange={(event) => onChange({
                    ...content,
                    chapters: updateAt(chapters, index, {
                      summary: event.target.value,
                    }),
                  })}
                  rows={4}
                  disabled={disabled}
                />
              </label>
            </article>
          ))
        : null}

      {artifact.artifact_kind === "timeline_fact_review"
        ? reviews.map((review, index) => (
            <article key={review.claim_id}>
              <div className="artifactEditTrace">
                <code>{review.target_path}</code>
                <span>{review.claim_text}</span>
              </div>
              <label>
                <span>复核状态</span>
                <select
                  value={review.status}
                  onChange={(event) => onChange({
                    ...content,
                    reviews: updateAt(reviews, index, {
                      status: event.target.value as FactReviewStatus,
                    }),
                  })}
                  disabled={disabled}
                >
                  {FACT_STATUSES.map((status) => (
                    <option key={status} value={status}>{status}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>说明</span>
                <textarea
                  value={review.explanation ?? ""}
                  onChange={(event) => onChange({
                    ...content,
                    reviews: updateAt(reviews, index, {
                      explanation: event.target.value || null,
                    }),
                  })}
                  rows={3}
                  disabled={disabled}
                />
              </label>
            </article>
          ))
        : null}

      <label>
        <span>Warnings（每行一条）</span>
        <textarea
          value={(content.warnings ?? []).join("\n")}
          onChange={(event) => onChange({
            ...content,
            warnings: event.target.value.split(/\r?\n/).filter(Boolean),
          })}
          rows={3}
          disabled={disabled}
        />
      </label>

      <footer>
        <span>保存会追加人工版本，不覆盖当前 Artifact。</span>
        <div>
          <button
            type="button"
            className="secondary"
            onClick={onCancel}
            disabled={disabled}
          >取消</button>
          <button type="button" onClick={onSave} disabled={disabled}>
            保存新版本
          </button>
        </div>
      </footer>
    </section>
  );
}
