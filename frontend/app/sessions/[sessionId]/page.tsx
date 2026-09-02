"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  approveArtifact,
  approveRevision,
  buildRevisionPackage,
  buildSessionPackage,
  cancelProcessingJob,
  createPackageRevision,
  createArtifactVersion,
  createProcessingJob,
  createRevisionVersion,
  getArtifact,
  getArtifactExportUrl,
  getPackage,
  getPackageExportUrl,
  getRevision,
  getRevisionExportUrl,
  getProcessingJob,
  getSegments,
  getSession,
  getTranslations,
  listSessionPackages,
  listSessionRevisions,
  listArtifactVersions,
  listPackageArtifacts,
  listPackageJobs,
  validatePackage,
} from "@/lib/api";
import { ArtifactReviewEditor } from "@/components/artifact-review-editor";
import type {
  ArtifactContent,
  ArtifactKind,
  ArtifactSection,
  DerivedArtifact,
  ProcessingJob,
} from "@/types/artifacts";
import type {
  PackageTranscriptItem,
  PackageSummary,
  PackageValidation,
  TranscriptPackage,
} from "@/types/packages";
import type {
  RevisionItem,
  RevisionSummary,
  TranscriptRevision,
} from "@/types/revisions";
import type { Segment, Session, TranslationSegment } from "@/types/session";


function formatTime(milliseconds: number | null): string {
  if (milliseconds === null) {
    return "--:--";
  }
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}


function copyItems(items: RevisionItem[]): RevisionItem[] {
  return items.map((item) => ({
    ...item,
    source_segment_ids: [...item.source_segment_ids],
  }));
}


function copyArtifactContent(content: ArtifactContent): ArtifactContent {
  return structuredClone(content);
}


function splitText(value: string): [string, string] | null {
  const text = value.trim();
  if (text.length < 2) {
    return null;
  }
  const midpoint = Math.floor(text.length / 2);
  const candidates = [
    text.lastIndexOf(" ", midpoint),
    text.lastIndexOf("，", midpoint) + 1,
    text.lastIndexOf("。", midpoint) + 1,
    text.indexOf(" ", midpoint),
    text.indexOf("，", midpoint) + 1,
    text.indexOf("。", midpoint) + 1,
  ].filter((position) => position > 0 && position < text.length);
  const offset = candidates.length > 0
    ? candidates.reduce((best, current) => (
        Math.abs(current - midpoint) < Math.abs(best - midpoint) ? current : best
      ))
    : midpoint;
  const first = text.slice(0, offset).trim();
  const second = text.slice(offset).trim();
  return first && second ? [first, second] : null;
}


function itemFingerprint(item: RevisionItem): string {
  return JSON.stringify({
    source_segment_ids: item.source_segment_ids,
    start_ms: item.start_ms,
    end_ms: item.end_ms,
    text: item.text,
  });
}


async function loadArtifactHistory(
  packages: PackageSummary[],
): Promise<DerivedArtifact[]> {
  const grouped = await Promise.all(
    packages.map((item) => listPackageArtifacts(item.package_id)),
  );
  return grouped.flat().sort((left, right) => (
    right.package_version - left.package_version
    || right.artifact_version - left.artifact_version
    || right.created_at.localeCompare(left.created_at)
  ));
}


function parseGlossary(value: string): Record<string, string> {
  const glossary: Record<string, string> = {};
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    const line = rawLine.trim();
    if (!line) {
      continue;
    }
    const separator = line.indexOf("=");
    const source = line.slice(0, separator).trim();
    const target = line.slice(separator + 1).trim();
    if (separator <= 0 || !source || !target) {
      throw new Error(`术语表第 ${index + 1} 行应为 source=target`);
    }
    if (Object.hasOwn(glossary, source)) {
      throw new Error(`术语表包含重复源词：${source}`);
    }
    glossary[source] = target;
  }
  return glossary;
}


function sameLanguage(left: string | null, right: string | null): boolean {
  return Boolean(
    left
    && right
    && left.toLocaleLowerCase() === right.toLocaleLowerCase(),
  );
}


function artifactText(section: ArtifactSection): string {
  return section.translated_text ?? section.clean_text ?? "";
}


function joinSourceItems(
  itemsById: Map<string, PackageTranscriptItem>,
  itemIds: string[],
): string {
  return itemIds
    .map((itemId) => itemsById.get(itemId)?.text ?? "")
    .filter(Boolean)
    .join(" ");
}


export default function SessionPackagePage() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = params.sessionId;
  const [session, setSession] = useState<Session | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [translations, setTranslations] = useState<TranslationSegment[]>([]);
  const [packages, setPackages] = useState<PackageSummary[]>([]);
  const [selectedPackage, setSelectedPackage] =
    useState<TranscriptPackage | null>(null);
  const [validation, setValidation] = useState<PackageValidation | null>(null);
  const [revisions, setRevisions] = useState<RevisionSummary[]>([]);
  const [selectedRevision, setSelectedRevision] =
    useState<TranscriptRevision | null>(null);
  const [parentRevision, setParentRevision] =
    useState<TranscriptRevision | null>(null);
  const [draftItems, setDraftItems] = useState<RevisionItem[]>([]);
  const [processingJobs, setProcessingJobs] = useState<ProcessingJob[]>([]);
  const [artifacts, setArtifacts] = useState<DerivedArtifact[]>([]);
  const [selectedArtifact, setSelectedArtifact] =
    useState<DerivedArtifact | null>(null);
  const [artifactVersions, setArtifactVersions] =
    useState<DerivedArtifact[]>([]);
  const [artifactDraft, setArtifactDraft] =
    useState<ArtifactContent | null>(null);
  const [artifactEditing, setArtifactEditing] = useState(false);
  const [refinedTargetLanguage, setRefinedTargetLanguage] = useState("");
  const [refinedStyle, setRefinedStyle] = useState("");
  const [refinedGlossary, setRefinedGlossary] = useState("");
  const [refinedContextWindow, setRefinedContextWindow] = useState(2);
  const [evidenceWorkflowStyle, setEvidenceWorkflowStyle] = useState("");
  const [factReviewTargetId, setFactReviewTargetId] = useState("");
  const [changeSummary, setChangeSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [processingBusy, setProcessingBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applySelectedRevision = useCallback(
    async (revision: TranscriptRevision | null) => {
      setSelectedRevision(revision);
      setDraftItems(revision === null ? [] : copyItems(revision.content.items));
      setChangeSummary("");
      if (revision?.parent_revision_id) {
        setParentRevision(await getRevision(revision.parent_revision_id));
      } else {
        setParentRevision(null);
      }
    },
    [],
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [
        nextSession,
        nextSegments,
        nextTranslations,
        nextPackages,
        nextRevisions,
      ] = await Promise.all([
        getSession(sessionId),
        getSegments(sessionId),
        getTranslations(sessionId),
        listSessionPackages(sessionId),
        listSessionRevisions(sessionId),
      ]);
      setSession(nextSession);
      setSegments(nextSegments);
      setTranslations(nextTranslations);
      setPackages(nextPackages);
      setRevisions(nextRevisions);
      const primaryPackage = nextPackages[0] ?? null;
      const [packageDetail, nextJobs, nextArtifacts] = await Promise.all([
        primaryPackage ? getPackage(primaryPackage.package_id) : null,
        primaryPackage ? listPackageJobs(primaryPackage.package_id) : [],
        loadArtifactHistory(nextPackages),
      ]);
      setSelectedPackage(packageDetail);
      setRefinedTargetLanguage((current) => current || (
        packageDetail?.manifest.target_languages[0]
        ?? nextSession.target_language
        ?? "en-US"
      ));
      setProcessingJobs(nextJobs);
      setArtifacts(nextArtifacts);
      const initialArtifact = (
        nextArtifacts.find(
          (item) => item.package_id === primaryPackage?.package_id,
        ) ?? nextArtifacts[0] ?? null
      );
      setSelectedArtifact(initialArtifact);
      setArtifactDraft(
        initialArtifact ? copyArtifactContent(initialArtifact.content) : null,
      );
      setArtifactEditing(false);
      setArtifactVersions(
        initialArtifact
          ? await listArtifactVersions(initialArtifact.artifact_id)
          : [],
      );
      await applySelectedRevision(
        nextRevisions.length > 0
          ? await getRevision(nextRevisions[0].revision_id)
          : null,
      );
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "加载失败");
    }
  }, [applySelectedRevision, sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const activeJobIds = useMemo(
    () => processingJobs
      .filter((item) => item.status === "queued" || item.status === "running")
      .map((item) => item.job_id)
      .join(","),
    [processingJobs],
  );

  useEffect(() => {
    if (!activeJobIds) {
      return;
    }
    let disposed = false;
    const poll = async () => {
      try {
        const updates = await Promise.all(
          activeJobIds.split(",").map((jobId) => getProcessingJob(jobId)),
        );
        if (disposed) {
          return;
        }
        setProcessingJobs((current) => current.map(
          (job) => updates.find((item) => item.job_id === job.job_id) ?? job,
        ));
        const completedIds = updates
          .filter((item) => item.status === "completed" && item.result_artifact_id)
          .map((item) => item.result_artifact_id as string);
        if (completedIds.length > 0) {
          const completedArtifacts = await Promise.all(
            completedIds.map((artifactId) => getArtifact(artifactId)),
          );
          const latestArtifact = completedArtifacts[0];
          const latestVersions = await listArtifactVersions(
            latestArtifact.artifact_id,
          );
          if (!disposed) {
            setArtifacts((current) => {
              const byId = new Map(
                current.map((artifact) => [artifact.artifact_id, artifact]),
              );
              for (const artifact of completedArtifacts) {
                byId.set(artifact.artifact_id, artifact);
              }
              return [...byId.values()].sort((left, right) => (
                right.package_version - left.package_version
                || right.artifact_version - left.artifact_version
              ));
            });
            setSelectedArtifact(latestArtifact);
            setArtifactDraft(copyArtifactContent(latestArtifact.content));
            setArtifactEditing(false);
            setArtifactVersions(latestVersions);
            if (
              latestArtifact.artifact_kind === "refined_translation"
              && latestArtifact.target_language
            ) {
              setRefinedTargetLanguage(latestArtifact.target_language);
            }
          }
        }
      } catch (pollError) {
        if (!disposed) {
          setError(
            pollError instanceof Error ? pollError.message : "刷新处理任务失败",
          );
        }
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 800);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeJobIds]);

  const translationGroups = useMemo(() => {
    const groups = new Map<string, TranslationSegment[]>();
    for (const item of translations) {
      groups.set(item.target_language, [
        ...(groups.get(item.target_language) ?? []),
        item,
      ]);
    }
    return [...groups.entries()];
  }, [translations]);

  const packageSourceItems = useMemo(() => {
    if (selectedPackage === null) {
      return [];
    }
    return selectedPackage.documents.find(
      (document) => document.document_id
        === selectedPackage.manifest.effective_source_document_id,
    )?.content.items ?? [];
  }, [selectedPackage]);

  const latestFrozenPackage = useMemo(() => packages
    .filter((item) => item.status === "frozen" && item.content_hash !== null)
    .sort((left, right) => right.package_version - left.package_version)[0]
    ?? null, [packages]);

  const selectedArtifactIsStale = Boolean(
    selectedArtifact
    && latestFrozenPackage
    && selectedArtifact.package_content_hash !== latestFrozenPackage.content_hash,
  );

  const factReviewTargets = useMemo(() => artifacts.filter((artifact) => (
    artifact.package_id === selectedPackage?.package_id
    && (
      artifact.artifact_kind === "summary"
      || artifact.artifact_kind === "clean_script"
      || artifact.artifact_kind === "refined_translation"
      || artifact.artifact_kind === "chapter_outline"
    )
  )), [artifacts, selectedPackage]);

  const activeFactReviewTargetId = factReviewTargets.some(
    (artifact) => artifact.artifact_id === factReviewTargetId,
  )
    ? factReviewTargetId
    : factReviewTargets[0]?.artifact_id ?? "";

  const comparison = useMemo(() => {
    if (selectedPackage === null) {
      return null;
    }
    const sourceItems = packageSourceItems;
    const sourceItemsById = new Map(
      sourceItems.map((item) => [item.item_id, item]),
    );
    const refinedArtifacts = artifacts.filter((artifact) => (
      artifact.package_id === selectedPackage.package_id
      && artifact.artifact_kind === "refined_translation"
    ));
    const comparisonArtifact = (
      selectedArtifact?.package_id === selectedPackage.package_id
      && selectedArtifact.artifact_kind === "refined_translation"
      && sameLanguage(selectedArtifact.target_language, refinedTargetLanguage)
    )
      ? selectedArtifact
      : refinedArtifacts.find((artifact) => sameLanguage(
          artifact.target_language,
          refinedTargetLanguage,
        )) ?? refinedArtifacts[0] ?? null;
    const targetLanguage = comparisonArtifact?.target_language
      ?? refinedTargetLanguage
      ?? selectedPackage.manifest.target_languages[0]
      ?? null;
    const liveDocument = selectedPackage.documents.find((document) => (
      document.document_kind === "live_translation"
      && sameLanguage(document.language, targetLanguage)
    ));
    const liveItems = liveDocument?.content.items ?? [];
    const finalSections = comparisonArtifact?.content.sections ?? [];
    const rows = finalSections.length > 0
      ? finalSections.map((section, index) => {
          const segmentIds = new Set(section.source_segment_ids);
          return {
            key: `${comparisonArtifact?.artifact_id ?? "final"}-${index}`,
            startMs: section.start_ms,
            endMs: section.end_ms,
            sourceItemIds: section.source_item_ids,
            sourceText: section.source_text
              ?? joinSourceItems(sourceItemsById, section.source_item_ids),
            liveText: liveItems
              .filter((item) => item.source_segment_ids.some(
                (segmentId) => segmentIds.has(segmentId),
              ))
              .map((item) => item.text)
              .join(" "),
            finalText: section.translated_text ?? "",
          };
        })
      : sourceItems.map((item) => {
          const segmentIds = new Set(item.source_segment_ids);
          return {
            key: item.item_id,
            startMs: item.start_ms,
            endMs: item.end_ms,
            sourceItemIds: [item.item_id],
            sourceText: item.text,
            liveText: liveItems
              .filter((liveItem) => liveItem.source_segment_ids.some(
                (segmentId) => segmentIds.has(segmentId),
              ))
              .map((liveItem) => liveItem.text)
              .join(" "),
            finalText: "",
          };
        });
    return {
      artifact: comparisonArtifact,
      targetLanguage,
      hasLiveTranslation: liveDocument !== undefined,
      rows,
    };
  }, [
    artifacts,
    packageSourceItems,
    refinedTargetLanguage,
    selectedArtifact,
    selectedPackage,
  ]);

  const revisionDiff = useMemo(() => {
    if (selectedRevision === null || parentRevision === null) {
      return null;
    }
    const previous = new Map(
      parentRevision.content.items.map((item) => [item.item_id, item]),
    );
    const current = new Map(
      selectedRevision.content.items.map((item) => [item.item_id, item]),
    );
    return {
      added: [...current.keys()].filter((itemId) => !previous.has(itemId)).length,
      removed: [...previous.keys()].filter((itemId) => !current.has(itemId)).length,
      changed: [...current.entries()].filter(([itemId, item]) => {
        const oldItem = previous.get(itemId);
        return oldItem !== undefined && itemFingerprint(oldItem) !== itemFingerprint(item);
      }).length,
    };
  }, [parentRevision, selectedRevision]);

  async function refreshPackages(selectedPackageId?: string) {
    const nextPackages = await listSessionPackages(sessionId);
    setPackages(nextPackages);
    const packageId = selectedPackageId ?? nextPackages[0]?.package_id;
    const [packageDetail, nextJobs, nextArtifacts] = await Promise.all([
      packageId ? getPackage(packageId) : null,
      packageId ? listPackageJobs(packageId) : [],
      loadArtifactHistory(nextPackages),
    ]);
    setSelectedPackage(packageDetail);
    setProcessingJobs(nextJobs);
    setArtifacts(nextArtifacts);
    const nextArtifact = (
      nextArtifacts.find((item) => item.package_id === packageId)
      ?? nextArtifacts[0]
      ?? null
    );
    setSelectedArtifact(nextArtifact);
    setArtifactDraft(
      nextArtifact ? copyArtifactContent(nextArtifact.content) : null,
    );
    setArtifactEditing(false);
    setArtifactVersions(
      nextArtifact
        ? await listArtifactVersions(nextArtifact.artifact_id)
        : [],
    );
  }

  async function refreshRevisions(selectedRevisionId?: string) {
    const nextRevisions = await listSessionRevisions(sessionId);
    setRevisions(nextRevisions);
    const revisionId = selectedRevisionId ?? nextRevisions[0]?.revision_id;
    await applySelectedRevision(
      revisionId ? await getRevision(revisionId) : null,
    );
  }

  async function buildBaselinePackage() {
    setBusy(true);
    setError(null);
    setValidation(null);
    try {
      const created = await buildSessionPackage(sessionId);
      await refreshPackages(created.package_id);
    } catch (buildError) {
      setError(buildError instanceof Error ? buildError.message : "构建失败");
    } finally {
      setBusy(false);
    }
  }

  async function inspectPackage(packageId: string) {
    setBusy(true);
    setError(null);
    setValidation(null);
    try {
      const [packageDetail, nextJobs] = await Promise.all([
        getPackage(packageId),
        listPackageJobs(packageId),
      ]);
      setSelectedPackage(packageDetail);
      setProcessingJobs(nextJobs);
      const nextArtifact = (
        artifacts.find((item) => item.package_id === packageId) ?? null
      );
      setSelectedArtifact(nextArtifact);
      setArtifactDraft(
        nextArtifact ? copyArtifactContent(nextArtifact.content) : null,
      );
      setArtifactEditing(false);
      setArtifactVersions(
        nextArtifact
          ? await listArtifactVersions(nextArtifact.artifact_id)
          : [],
      );
    } catch (inspectError) {
      setError(inspectError instanceof Error ? inspectError.message : "读取失败");
    } finally {
      setBusy(false);
    }
  }

  async function createCleanScriptJob() {
    if (selectedPackage === null) {
      return;
    }
    setProcessingBusy(true);
    setError(null);
    try {
      const created = await createProcessingJob(
        selectedPackage.package_id,
        "clean_script",
      );
      setProcessingJobs((current) => [
        created,
        ...current.filter((item) => item.job_id !== created.job_id),
      ]);
    } catch (processingError) {
      setError(
        processingError instanceof Error
          ? processingError.message
          : "创建台本处理任务失败",
      );
    } finally {
      setProcessingBusy(false);
    }
  }

  async function createRefinedTranslationJob() {
    if (selectedPackage === null) {
      return;
    }
    const targetLanguage = refinedTargetLanguage.trim();
    if (!/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(targetLanguage)) {
      setError("目标语言必须是类似 en-US、ja-JP 的语言标签");
      return;
    }
    setProcessingBusy(true);
    setError(null);
    try {
      const glossary = parseGlossary(refinedGlossary);
      const created = await createProcessingJob(
        selectedPackage.package_id,
        "refined_translation",
        {
          target_language: targetLanguage,
          glossary,
          ...(refinedStyle.trim() ? { style: refinedStyle.trim() } : {}),
          context_window_items: refinedContextWindow,
        },
      );
      setProcessingJobs((current) => [
        created,
        ...current.filter((item) => item.job_id !== created.job_id),
      ]);
    } catch (processingError) {
      setError(
        processingError instanceof Error
          ? processingError.message
          : "创建最终翻译任务失败",
      );
    } finally {
      setProcessingBusy(false);
    }
  }

  async function createEvidenceWorkflowJob(
    artifactKind: Extract<ArtifactKind, "summary" | "chapter_outline">,
  ) {
    if (selectedPackage === null) {
      return;
    }
    setProcessingBusy(true);
    setError(null);
    try {
      const style = evidenceWorkflowStyle.trim();
      const created = await createProcessingJob(
        selectedPackage.package_id,
        artifactKind,
        style ? { style } : {},
      );
      setProcessingJobs((current) => [
        created,
        ...current.filter((item) => item.job_id !== created.job_id),
      ]);
    } catch (processingError) {
      setError(
        processingError instanceof Error
          ? processingError.message
          : "创建证据工作流任务失败",
      );
    } finally {
      setProcessingBusy(false);
    }
  }

  async function createFactReviewJob() {
    if (selectedPackage === null || !activeFactReviewTargetId) {
      setError("请先选择当前 Package 的目标 Artifact");
      return;
    }
    setProcessingBusy(true);
    setError(null);
    try {
      const created = await createProcessingJob(
        selectedPackage.package_id,
        "timeline_fact_review",
        {},
        activeFactReviewTargetId,
      );
      setProcessingJobs((current) => [
        created,
        ...current.filter((item) => item.job_id !== created.job_id),
      ]);
    } catch (processingError) {
      setError(
        processingError instanceof Error
          ? processingError.message
          : "创建事实复核任务失败",
      );
    } finally {
      setProcessingBusy(false);
    }
  }

  async function inspectArtifact(artifact: DerivedArtifact) {
    setBusy(true);
    setError(null);
    try {
      const versionsPromise = listArtifactVersions(artifact.artifact_id);
      if (artifact.package_id !== selectedPackage?.package_id) {
        const [packageDetail, nextJobs] = await Promise.all([
          getPackage(artifact.package_id),
          listPackageJobs(artifact.package_id),
        ]);
        setSelectedPackage(packageDetail);
        setProcessingJobs(nextJobs);
      }
      setSelectedArtifact(artifact);
      setArtifactDraft(copyArtifactContent(artifact.content));
      setArtifactEditing(false);
      setArtifactVersions(await versionsPromise);
      if (
        artifact.artifact_kind === "refined_translation"
        && artifact.target_language
      ) {
        setRefinedTargetLanguage(artifact.target_language);
      }
    } catch (inspectError) {
      setError(
        inspectError instanceof Error ? inspectError.message : "读取 Artifact 失败",
      );
    } finally {
      setBusy(false);
    }
  }

  function beginArtifactEdit() {
    if (selectedArtifact === null) {
      return;
    }
    setArtifactDraft(copyArtifactContent(selectedArtifact.content));
    setArtifactEditing(true);
  }

  function cancelArtifactEdit() {
    setArtifactDraft(
      selectedArtifact
        ? copyArtifactContent(selectedArtifact.content)
        : null,
    );
    setArtifactEditing(false);
  }

  async function saveArtifactVersion() {
    if (selectedArtifact === null || artifactDraft === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createArtifactVersion(
        selectedArtifact.artifact_id,
        artifactDraft,
      );
      const [nextArtifacts, nextVersions] = await Promise.all([
        loadArtifactHistory(packages),
        listArtifactVersions(created.artifact_id),
      ]);
      setArtifacts(nextArtifacts);
      setSelectedArtifact(created);
      setArtifactDraft(copyArtifactContent(created.content));
      setArtifactVersions(nextVersions);
      setArtifactEditing(false);
    } catch (saveError) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "保存 Artifact 人工版本失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function approveSelectedArtifact() {
    if (selectedArtifact === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const approved = await approveArtifact(selectedArtifact.artifact_id);
      const [nextArtifacts, nextVersions] = await Promise.all([
        loadArtifactHistory(packages),
        listArtifactVersions(approved.artifact_id),
      ]);
      setArtifacts(nextArtifacts);
      setSelectedArtifact(approved);
      setArtifactDraft(copyArtifactContent(approved.content));
      setArtifactVersions(nextVersions);
      setArtifactEditing(false);
    } catch (approveError) {
      setError(
        approveError instanceof Error
          ? approveError.message
          : "批准 Artifact 失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function regenerateSelectedArtifact() {
    if (selectedArtifact === null) {
      return;
    }
    const targetArtifactId = selectedArtifact.artifact_kind
      === "timeline_fact_review"
      ? selectedArtifact.content.target_artifact?.artifact_id
      : undefined;
    const generationOptions = Object.fromEntries(
      Object.entries(selectedArtifact.options).filter(
        ([key]) => key !== "human_edit",
      ),
    );
    setProcessingBusy(true);
    setError(null);
    try {
      const created = await createProcessingJob(
        selectedArtifact.package_id,
        selectedArtifact.artifact_kind,
        selectedArtifact.artifact_kind === "timeline_fact_review"
          ? {}
          : generationOptions,
        targetArtifactId,
      );
      setProcessingJobs((current) => [
        created,
        ...current.filter((item) => item.job_id !== created.job_id),
      ]);
    } catch (regenerateError) {
      setError(
        regenerateError instanceof Error
          ? regenerateError.message
          : "重新生成 Artifact 失败",
      );
    } finally {
      setProcessingBusy(false);
    }
  }

  async function cancelJob(jobId: string) {
    setProcessingBusy(true);
    setError(null);
    try {
      const cancelled = await cancelProcessingJob(jobId);
      setProcessingJobs((current) => current.map(
        (item) => item.job_id === cancelled.job_id ? cancelled : item,
      ));
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "取消任务失败");
    } finally {
      setProcessingBusy(false);
    }
  }

  async function runValidation() {
    if (selectedPackage === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setValidation(await validatePackage(selectedPackage.package_id));
    } catch (validationError) {
      setError(
        validationError instanceof Error ? validationError.message : "校验失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function createRevision() {
    if (selectedPackage === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createPackageRevision(selectedPackage.package_id);
      await refreshRevisions(created.revision_id);
    } catch (revisionError) {
      setError(
        revisionError instanceof Error ? revisionError.message : "创建校对稿失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function inspectRevision(revisionId: string) {
    setBusy(true);
    setError(null);
    try {
      await applySelectedRevision(await getRevision(revisionId));
    } catch (revisionError) {
      setError(
        revisionError instanceof Error ? revisionError.message : "读取校对版本失败",
      );
    } finally {
      setBusy(false);
    }
  }

  function updateDraftItem(
    index: number,
    patch: Partial<Pick<RevisionItem, "text" | "start_ms" | "end_ms">>,
  ) {
    setDraftItems((current) => current.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch } : item
    )));
  }

  function mergeWithPrevious(index: number) {
    if (index <= 0) {
      return;
    }
    setDraftItems((current) => {
      const previous = current[index - 1];
      const item = current[index];
      const merged: RevisionItem = {
        item_id: crypto.randomUUID(),
        source_segment_ids: [...new Set([
          ...previous.source_segment_ids,
          ...item.source_segment_ids,
        ])],
        start_ms: Math.min(previous.start_ms, item.start_ms),
        end_ms: Math.max(previous.end_ms, item.end_ms),
        text: `${previous.text.trim()}\n${item.text.trim()}`,
      };
      return [
        ...current.slice(0, index - 1),
        merged,
        ...current.slice(index + 1),
      ];
    });
  }

  function splitItem(index: number) {
    const item = draftItems[index];
    const parts = splitText(item.text);
    if (parts === null) {
      setError("当前条目文字太短，无法自动拆分。可先补充文字再拆分。");
      return;
    }
    const midpoint = Math.floor((item.start_ms + item.end_ms) / 2);
    setDraftItems((current) => [
      ...current.slice(0, index),
      {
        ...item,
        item_id: crypto.randomUUID(),
        end_ms: midpoint,
        text: parts[0],
      },
      {
        ...item,
        item_id: crypto.randomUUID(),
        start_ms: midpoint,
        text: parts[1],
      },
      ...current.slice(index + 1),
    ]);
  }

  async function saveRevisionVersion(
    sourceItems: RevisionItem[] = draftItems,
    summary: string = changeSummary,
  ) {
    if (selectedRevision === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const orderedItems = copyItems(sourceItems).sort(
        (left, right) => left.start_ms - right.start_ms || left.end_ms - right.end_ms,
      );
      const created = await createRevisionVersion(
        selectedRevision.revision_id,
        { items: orderedItems },
        summary || `基于 Revision v${selectedRevision.version} 保存`,
      );
      await refreshRevisions(created.revision_id);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存版本失败");
    } finally {
      setBusy(false);
    }
  }

  async function approveSelectedRevision() {
    if (selectedRevision === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const approved = await approveRevision(selectedRevision.revision_id);
      await refreshRevisions(approved.revision_id);
    } catch (approveError) {
      setError(
        approveError instanceof Error ? approveError.message : "批准版本失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function buildApprovedPackage() {
    if (selectedRevision === null) {
      return;
    }
    setBusy(true);
    setError(null);
    setValidation(null);
    try {
      const created = await buildRevisionPackage(selectedRevision.revision_id);
      await refreshPackages(created.package_id);
    } catch (packageError) {
      setError(
        packageError instanceof Error ? packageError.message : "构建 Package v2 失败",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="packageShell">
      <header className="packageHero">
        <div>
          <span className="label">Stage 2E · Evidence-grounded processing</span>
          <h1>Session 后处理</h1>
          <p>冻结与校对字幕，通过 Package 生成台本、精译、摘要、章节和包内事实复核。</p>
        </div>
        <Link href="/" className="packageBackLink">返回 Room 工作台</Link>
      </header>

      {error !== null ? <p className="packageError">{error}</p> : null}

      <section className="packageOverviewGrid">
        <article className="packageCard">
          <span className="label">Session</span>
          {session === null ? (
            <p className="emptyState">正在读取 Session…</p>
          ) : (
            <dl className="packageFacts">
              <div><dt>ID</dt><dd><code>{session.id}</code></dd></div>
              <div><dt>状态</dt><dd>{session.status}</dd></div>
              <div><dt>输入</dt><dd>{session.source_type}</dd></div>
              <div><dt>语言</dt><dd>{session.language}</dd></div>
              <div><dt>目标语言</dt><dd>{session.target_language ?? "未启用"}</dd></div>
              <div><dt>Final</dt><dd>{segments.length}</dd></div>
            </dl>
          )}
        </article>

        <article className="packageCard packageBuildCard">
          <span className="label">Baseline package</span>
          <h2>冻结当前 Stage 1 Final</h2>
          <p>这是唯一读取 Stage 1 Final 的入口；每次构建新增版本。</p>
          <button
            type="button"
            onClick={() => void buildBaselinePackage()}
            disabled={busy}
          >
            {busy ? "处理中…" : "构建 baseline Package"}
          </button>
        </article>
      </section>

      <section className="packageWorkspace">
        <aside className="packageCard packageVersionPanel">
          <div className="packageSectionHeading">
            <div><span className="label">Packages</span><h2>成果包版本</h2></div>
            <strong>{packages.length}</strong>
          </div>
          {packages.length === 0 ? (
            <p className="emptyState">尚未构建成果包。</p>
          ) : (
            <div className="packageVersionList">
              {packages.map((item) => (
                <button
                  type="button"
                  key={item.package_id}
                  className={selectedPackage?.package_id === item.package_id ? "active" : ""}
                  onClick={() => void inspectPackage(item.package_id)}
                  disabled={busy}
                >
                  <span><strong>Package v{item.package_version}</strong><i>{item.status}</i></span>
                  <code>{item.content_hash?.slice(0, 16) ?? "building"}</code>
                </button>
              ))}
            </div>
          )}
        </aside>

        <article className="packageCard packageDetailPanel">
          {selectedPackage === null ? (
            <p className="emptyState">选择或构建一个 Package 查看详情。</p>
          ) : (
            <>
              <div className="packageSectionHeading">
                <div>
                  <span className="label">Package v{selectedPackage.package_version}</span>
                  <h2>{selectedPackage.status}</h2>
                </div>
                <div className="packageDetailActions">
                  <button type="button" className="secondary" onClick={() => void createRevision()} disabled={busy}>创建校对稿</button>
                  <button type="button" className="secondary" onClick={() => void runValidation()} disabled={busy}>校验</button>
                  <a href={getPackageExportUrl(selectedPackage.package_id)}>下载 ZIP</a>
                </div>
              </div>
              <code className="packageHash">sha256:{selectedPackage.content_hash}</code>
              <p className="packageEffectiveSource">
                有效源文档：<code>{selectedPackage.manifest.effective_source_document_id}</code>
                {selectedPackage.source_revision_id
                  ? ` · Revision ${selectedPackage.source_revision_id}`
                  : " · Stage 1 raw"}
              </p>
              <div className="packageDocumentGrid">
                {selectedPackage.documents.map((document) => (
                  <div key={document.document_id}>
                    <strong>{document.document_kind}</strong>
                    <span>{document.language ?? "package"}</span>
                    <small>{document.content.items?.length ?? 1} 项</small>
                  </div>
                ))}
              </div>
              {validation !== null ? (
                <p className={validation.valid ? "packageValidation valid" : "packageValidation invalid"}>
                  {validation.valid ? "Schema、文档哈希、Package 哈希和证据索引均有效。" : validation.errors.join("；")}
                </p>
              ) : null}
            </>
          )}
        </article>
      </section>

      <section className="artifactWorkspace">
        <aside className="packageCard artifactControlPanel">
          <div className="packageSectionHeading">
            <div>
              <span className="label">Stage 2F · Artifact workspace</span>
              <h2>离线处理任务</h2>
            </div>
          </div>
          <div className="artifactJobActions">
            <button
              type="button"
              className="secondary"
              onClick={() => void createCleanScriptJob()}
              disabled={selectedPackage === null || processingBusy}
            >
              {processingBusy ? "提交中…" : "生成整理版台本"}
            </button>
          </div>
          {selectedPackage !== null ? (
            <div className="artifactInputIdentity">
              <span>输入 Package v{selectedPackage.package_version}</span>
              <code>sha256:{selectedPackage.content_hash}</code>
            </div>
          ) : (
            <p className="emptyState">先选择一个 Frozen Package。</p>
          )}
          <form
            className="refinedTranslationForm"
            onSubmit={(event) => {
              event.preventDefault();
              void createRefinedTranslationJob();
            }}
          >
            <div className="refinedFormHeading">
              <div>
                <span className="label">Hybrid translation</span>
                <h3>生成最终译文</h3>
              </div>
              <span>源文为事实依据</span>
            </div>
            <label>
              <span>目标语言</span>
              <input
                value={refinedTargetLanguage}
                onChange={(event) => setRefinedTargetLanguage(event.target.value)}
                placeholder="en-US"
                maxLength={32}
                required
              />
            </label>
            <label>
              <span>文体（可选）</span>
              <input
                value={refinedStyle}
                onChange={(event) => setRefinedStyle(event.target.value)}
                placeholder="formal / broadcast / concise"
                maxLength={100}
              />
            </label>
            <label>
              <span>术语表（每行 source=target）</span>
              <textarea
                value={refinedGlossary}
                onChange={(event) => setRefinedGlossary(event.target.value)}
                placeholder={"百炼=Bailian\n台本=transcript"}
                rows={4}
              />
            </label>
            <label>
              <span>上下文窗口（条）</span>
              <input
                type="number"
                min={0}
                max={20}
                value={refinedContextWindow}
                onChange={(event) => setRefinedContextWindow(
                  Math.min(20, Math.max(0, Number(event.target.value))),
                )}
              />
            </label>
            <button
              type="submit"
              disabled={selectedPackage === null || processingBusy}
            >
              {processingBusy ? "提交中…" : "生成最终译文"}
            </button>
          </form>
          <section className="stage2eWorkflowControls">
            <div className="refinedFormHeading">
              <div>
                <span className="label">Evidence workflows</span>
                <h3>摘要与章节</h3>
              </div>
              <span>Package-only</span>
            </div>
            <label>
              <span>文体（可选）</span>
              <input
                value={evidenceWorkflowStyle}
                onChange={(event) => setEvidenceWorkflowStyle(event.target.value)}
                placeholder="concise / editorial"
                maxLength={100}
              />
            </label>
            <div className="stage2eActionRow">
              <button
                type="button"
                className="secondary"
                onClick={() => void createEvidenceWorkflowJob("summary")}
                disabled={selectedPackage === null || processingBusy}
              >生成摘要</button>
              <button
                type="button"
                className="secondary"
                onClick={() => void createEvidenceWorkflowJob("chapter_outline")}
                disabled={selectedPackage === null || processingBusy}
              >生成章节</button>
            </div>
            <div className="factReviewControl">
              <label>
                <span>事实复核目标</span>
                <select
                  value={activeFactReviewTargetId}
                  onChange={(event) => setFactReviewTargetId(event.target.value)}
                  disabled={factReviewTargets.length === 0 || processingBusy}
                >
                  {factReviewTargets.map((artifact) => (
                    <option key={artifact.artifact_id} value={artifact.artifact_id}>
                      {artifact.artifact_kind} v{artifact.artifact_version}
                      {artifact.target_language ? ` · ${artifact.target_language}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() => void createFactReviewJob()}
                disabled={!activeFactReviewTargetId || processingBusy}
              >执行包内事实复核</button>
              {factReviewTargets.length === 0 ? (
                <small>先为当前 Package 生成摘要、台本、精译或章节。</small>
              ) : null}
            </div>
          </section>
          <div className="processingJobList">
            {processingJobs.map((job) => (
              <article key={job.job_id} className={`processingJob job-${job.status}`}>
                <header>
                  <strong>{job.artifact_kind}</strong>
                  <i>{job.status}</i>
                </header>
                <progress max={100} value={job.progress}>{job.progress}%</progress>
                <small>
                  {job.provider ?? "provider"} · {job.model ?? "model"} · {job.progress}%
                  {typeof job.options.target_language === "string"
                    ? ` · ${job.options.target_language}`
                    : ""}
                </small>
                {job.error_code ? (
                  <p><code>{job.error_code}</code> {job.error_message}</p>
                ) : null}
                {job.status === "queued" || job.status === "running" ? (
                  <button
                    type="button"
                    className="secondary"
                    disabled={processingBusy}
                    onClick={() => void cancelJob(job.job_id)}
                  >取消任务</button>
                ) : null}
              </article>
            ))}
            {processingJobs.length === 0 ? (
              <p className="emptyState">当前 Package 尚无处理任务。</p>
            ) : null}
          </div>
        </aside>

        <aside className="packageCard artifactHistoryPanel">
          <div className="packageSectionHeading">
            <div><span className="label">Artifact history</span><h2>历史产物</h2></div>
            <strong>{artifacts.length}</strong>
          </div>
          <div className="artifactHistoryList">
            {artifacts.map((artifact) => (
              <button
                type="button"
                key={artifact.artifact_id}
                className={selectedArtifact?.artifact_id === artifact.artifact_id ? "active" : ""}
                onClick={() => void inspectArtifact(artifact)}
                disabled={busy}
              >
                <span>
                  <strong>{artifact.artifact_kind} v{artifact.artifact_version}</strong>
                  <i>{artifact.status}</i>
                </span>
                <small>
                  Package v{artifact.package_version} · {
                    artifact.created_by === "human"
                      ? "人工修订"
                      : artifact.provider ?? "迁移"
                  }
                  {artifact.target_language ? ` · ${artifact.target_language}` : ""}
                </small>
                <code>{artifact.package_content_hash.slice(0, 16)}</code>
              </button>
            ))}
          </div>
          {artifacts.length === 0 ? (
            <p className="emptyState">Session 尚无 Artifact；迁移后的旧台本也会显示在这里。</p>
          ) : null}
        </aside>

        <article className="packageCard artifactDetailPanel processedScriptPanel">
          {selectedArtifact === null ? (
            <p className="emptyState">选择历史 Artifact 或创建新的整理版台本。</p>
          ) : (
            <>
              <div className="scriptHeading">
                <div>
                  <span className="label">Package v{selectedArtifact.package_version} · Artifact v{selectedArtifact.artifact_version}</span>
                  <h2>{selectedArtifact.content.title ?? selectedArtifact.artifact_kind}</h2>
                  <p>
                    {selectedArtifact.created_by === "human"
                      ? "人工修订"
                      : `${selectedArtifact.provider ?? "unknown"} / ${selectedArtifact.model ?? "unknown"}`}
                    {" · "}workflow {selectedArtifact.workflow_version}
                  </p>
                </div>
                <div className="artifactHeadingActions">
                  <i className={`artifactStatus artifactStatus-${selectedArtifact.status}`}>
                    {selectedArtifact.status}
                  </i>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => void regenerateSelectedArtifact()}
                    disabled={processingBusy}
                  >重新生成</button>
                  <button
                    type="button"
                    className="secondary"
                    onClick={beginArtifactEdit}
                    disabled={busy || artifactEditing}
                  >人工修改</button>
                  <button
                    type="button"
                    onClick={() => void approveSelectedArtifact()}
                    disabled={
                      busy
                      || artifactEditing
                      || selectedArtifact.status === "approved"
                      || selectedArtifact.status === "superseded"
                      || selectedArtifact.status === "failed"
                    }
                  >批准</button>
                  <nav className="artifactExportLinks" aria-label="Artifact exports">
                    {(selectedArtifact.artifact_kind === "refined_translation"
                      ? ["json", "markdown", "srt", "vtt"] as const
                      : selectedArtifact.artifact_kind === "clean_script"
                        ? ["json", "markdown"] as const
                        : ["json"] as const
                    ).map((format) => (
                      <a
                        key={format}
                        href={getArtifactExportUrl(selectedArtifact.artifact_id, format)}
                      >{format}</a>
                    ))}
                  </nav>
                </div>
              </div>
              <code className="packageHash">sha256:{selectedArtifact.package_content_hash}</code>
              <div className="artifactIdentitySummary">
                <span>Identity</span>
                <code>{selectedArtifact.identity_key}</code>
                <span>
                  {selectedArtifact.created_by === "human"
                    ? `人工版本 · 父版本 ${selectedArtifact.parent_artifact_id?.slice(0, 8) ?? "--"}`
                    : "模型生成版本"}
                </span>
              </div>
              {selectedArtifactIsStale && latestFrozenPackage ? (
                <p className="artifactStaleNotice">
                  该成果基于 Package v{selectedArtifact.package_version}；当前最新为
                  v{latestFrozenPackage.package_version}。
                </p>
              ) : null}
              {artifactVersions.length > 0 ? (
                <nav className="artifactVersionTrail" aria-label="Artifact version history">
                  <span>版本链</span>
                  {artifactVersions.map((artifact) => (
                    <button
                      key={artifact.artifact_id}
                      type="button"
                      className={
                        artifact.artifact_id === selectedArtifact.artifact_id
                          ? "active"
                          : ""
                      }
                      onClick={() => void inspectArtifact(artifact)}
                      disabled={busy}
                    >v{artifact.artifact_version} · {artifact.status}</button>
                  ))}
                </nav>
              ) : null}
              {artifactEditing && artifactDraft ? (
                <ArtifactReviewEditor
                  artifact={selectedArtifact}
                  content={artifactDraft}
                  disabled={busy}
                  onChange={setArtifactDraft}
                  onCancel={cancelArtifactEdit}
                  onSave={() => void saveArtifactVersion()}
                />
              ) : (
                <>
              <ol className="scriptSections">
                {(selectedArtifact.content.sections ?? []).map((section, index) => (
                  <li key={`${selectedArtifact.artifact_id}-${index}`}>
                    <div className="scriptTrace">
                      <time>{formatTime(section.start_ms)}–{formatTime(section.end_ms)}</time>
                      <span>Package items</span>
                      {section.source_item_ids.map((itemId) => (
                        <code key={itemId}>{itemId.slice(0, 8)}</code>
                      ))}
                    </div>
                    {section.source_text ? (
                      <blockquote>{section.source_text}</blockquote>
                    ) : null}
                    <p>{artifactText(section)}</p>
                    <details>
                      <summary>证据 Segment</summary>
                      <code>{section.source_segment_ids.join(", ")}</code>
                    </details>
                    {section.notes.length > 0 ? (
                      <ul className="scriptNotes">
                        {section.notes.map((note) => <li key={note}>{note}</li>)}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ol>
              {selectedArtifact.artifact_kind === "summary" ? (
                <section className="summaryArtifactPanel">
                  <p className="summaryBrief">{selectedArtifact.content.brief}</p>
                  <ol className="evidenceResultList">
                    {(selectedArtifact.content.key_points ?? []).map((point, index) => (
                      <li key={`${selectedArtifact.artifact_id}-point-${index}`}>
                        <div className="evidenceResultHeading">
                          <strong>要点 {index + 1}</strong>
                          <time>{formatTime(point.start_ms)}–{formatTime(point.end_ms)}</time>
                        </div>
                        <p>{point.text}</p>
                        <nav className="packageItemLinks" aria-label="摘要证据">
                          {point.evidence_item_ids.map((itemId) => (
                            <a key={itemId} href={`#package-item-${itemId}`}>
                              {itemId.slice(0, 8)}
                            </a>
                          ))}
                        </nav>
                        <details>
                          <summary>证据摘录</summary>
                          <ul>{point.evidence_excerpts.map((excerpt) => (
                            <li key={excerpt.item_id}>{excerpt.text}</li>
                          ))}</ul>
                        </details>
                      </li>
                    ))}
                  </ol>
                </section>
              ) : null}
              {selectedArtifact.artifact_kind === "chapter_outline" ? (
                <ol className="chapterTimeline">
                  {(selectedArtifact.content.chapters ?? []).map((chapter, index) => (
                    <li key={`${selectedArtifact.artifact_id}-chapter-${index}`}>
                      <div className="chapterTimeRail">
                        <span>{index + 1}</span>
                        <time>{formatTime(chapter.start_ms)}–{formatTime(chapter.end_ms)}</time>
                      </div>
                      <div className="chapterBody">
                        <h3>{chapter.title}</h3>
                        <p>{chapter.summary}</p>
                        <nav className="packageItemLinks" aria-label="章节证据">
                          {chapter.evidence_item_ids.map((itemId) => (
                            <a key={itemId} href={`#package-item-${itemId}`}>
                              {itemId.slice(0, 8)}
                            </a>
                          ))}
                        </nav>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : null}
              {selectedArtifact.artifact_kind === "timeline_fact_review" ? (
                <section className="factReviewPanel">
                  {selectedArtifact.content.target_artifact ? (
                    <p className="factReviewTarget">
                      目标：{selectedArtifact.content.target_artifact.artifact_kind}
                      {" "}v{selectedArtifact.content.target_artifact.artifact_version}
                      {" · "}<code>{selectedArtifact.content.target_artifact.artifact_id.slice(0, 8)}</code>
                    </p>
                  ) : null}
                  <ol className="factReviewList">
                    {(selectedArtifact.content.reviews ?? []).map((review) => (
                      <li key={review.claim_id}>
                        <div className="factReviewHeading">
                          <code>{review.target_path}</code>
                          <i className={`factStatus factStatus-${review.status}`}>
                            {review.status}
                          </i>
                        </div>
                        <p>{review.claim_text}</p>
                        {review.explanation ? <small>{review.explanation}</small> : null}
                        <div className="factReviewEvidence">
                          <time>{formatTime(review.start_ms)}–{formatTime(review.end_ms)}</time>
                          <nav className="packageItemLinks" aria-label="事实复核证据">
                            {review.evidence_item_ids.map((itemId) => (
                              <a key={itemId} href={`#package-item-${itemId}`}>
                                {itemId.slice(0, 8)}
                              </a>
                            ))}
                          </nav>
                        </div>
                        {review.evidence_excerpts.length > 0 ? (
                          <details>
                            <summary>证据文本</summary>
                            <ul>{review.evidence_excerpts.map((excerpt) => (
                              <li key={excerpt.item_id}>{excerpt.text}</li>
                            ))}</ul>
                          </details>
                        ) : (
                          <p className="noFactEvidence">该状态没有绑定 Package 证据。</p>
                        )}
                      </li>
                    ))}
                  </ol>
                </section>
              ) : null}
              {(selectedArtifact.content.warnings ?? []).length > 0 ? (
                <div className="scriptWarnings">
                  <strong>Warnings</strong>
                  <ul>{selectedArtifact.content.warnings?.map((warning) => <li key={warning}>{warning}</li>)}</ul>
                </div>
              ) : null}
                </>
              )}
            </>
          )}
        </article>
      </section>

      <section className="packageCard translationComparison">
        <div className="translationComparisonHeading">
          <div>
            <span className="label">Translation comparison</span>
            <h2>源字幕 · 实时译文 · 最终译文</h2>
          </div>
          {comparison === null ? null : (
            <div className="comparisonMeta">
              <span>Package v{selectedPackage?.package_version}</span>
              <span>{comparison.targetLanguage ?? "未选择目标语言"}</span>
              <span>
                {comparison.artifact
                  ? `Artifact v${comparison.artifact.artifact_version}`
                  : "尚无最终译文"}
              </span>
              <span>
                {comparison.artifact?.model ?? "model pending"}
              </span>
            </div>
          )}
        </div>
        {comparison === null || comparison.rows.length === 0 ? (
          <p className="emptyState">选择含有效源文档的 Package 后查看翻译对照。</p>
        ) : (
          <>
            {!comparison.hasLiveTranslation ? (
              <p className="comparisonNotice">
                当前 Package 没有 {comparison.targetLanguage} 实时译文；最终译文仍可仅依据源文生成。
              </p>
            ) : null}
            <div className="translationComparisonScroller">
              <div className="comparisonHeader comparisonGrid">
                <span>时间</span>
                <strong>源字幕（事实源）</strong>
                <strong>实时译文（参考）</strong>
                <strong>最终译文</strong>
              </div>
              {comparison.rows.map((row) => (
                <article className="comparisonRow comparisonGrid" key={row.key}>
                  <time>{formatTime(row.startMs)}–{formatTime(row.endMs)}</time>
                  <div className="comparisonCell source"><p>{row.sourceText}</p></div>
                  <div className="comparisonCell live">
                    <p>{row.liveText || "—"}</p>
                  </div>
                  <div className="comparisonCell final">
                    <p>{row.finalText || "等待生成"}</p>
                    <div className="comparisonEvidence">
                      {row.sourceItemIds.map((itemId) => (
                        <code key={itemId}>{itemId.slice(0, 8)}</code>
                      ))}
                    </div>
                  </div>
                </article>
              ))}
            </div>
            {(comparison.artifact?.content.warnings ?? []).length > 0 ? (
              <div className="comparisonWarnings">
                <strong>Warnings</strong>
                <ul>
                  {comparison.artifact?.content.warnings?.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        )}
      </section>

      <section className="revisionWorkspace">
        <aside className="packageCard revisionHistoryPanel">
          <div className="packageSectionHeading">
            <div><span className="label">Revision history</span><h2>校对版本</h2></div>
            <strong>{revisions.length}</strong>
          </div>
          {revisions.length === 0 ? (
            <p className="emptyState">先从一个 Package 创建校对稿。</p>
          ) : (
            <div className="revisionHistoryList">
              {revisions.map((revision) => (
                <button
                  type="button"
                  key={revision.revision_id}
                  className={selectedRevision?.revision_id === revision.revision_id ? "active" : ""}
                  onClick={() => void inspectRevision(revision.revision_id)}
                  disabled={busy}
                >
                  <span><strong>Revision v{revision.version}</strong><i>{revision.status}</i></span>
                  <small>{revision.item_count} 项 · {revision.change_summary ?? "无摘要"}</small>
                  <code>{revision.content_hash.slice(0, 16)}</code>
                </button>
              ))}
            </div>
          )}
          {selectedRevision !== null ? (
            <div className="revisionExports">
              <span>导出当前 Revision</span>
              {(["srt", "vtt", "markdown"] as const).map((format) => (
                <a key={format} href={getRevisionExportUrl(selectedRevision.revision_id, format)}>{format}</a>
              ))}
            </div>
          ) : null}
        </aside>

        <article className="packageCard revisionEditorPanel">
          {selectedRevision === null ? (
            <p className="emptyState">选择或创建 Revision 后开始校对。</p>
          ) : (
            <>
              <div className="revisionEditorHeader">
                <div>
                  <span className="label">Revision v{selectedRevision.version}</span>
                  <h2>校对编辑器</h2>
                  <p>{selectedRevision.language} · {selectedRevision.status} · 基于 Package {selectedRevision.base_package_id.slice(0, 8)}</p>
                </div>
                <div className="revisionPrimaryActions">
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => void saveRevisionVersion(selectedRevision.content.items, `恢复 Revision v${selectedRevision.version}`)}
                    disabled={busy}
                  >恢复为新版本</button>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => void approveSelectedRevision()}
                    disabled={busy || selectedRevision.status !== "saved"}
                  >批准版本</button>
                  <button
                    type="button"
                    onClick={() => void buildApprovedPackage()}
                    disabled={busy || selectedRevision.status !== "approved"}
                  >构建 Package vNext</button>
                </div>
              </div>

              <label className="revisionSummaryField">
                <span>本次修改摘要</span>
                <input
                  value={changeSummary}
                  onChange={(event) => setChangeSummary(event.target.value)}
                  placeholder="例如：修正人名并合并重复字幕"
                  maxLength={1000}
                />
              </label>

              <div className="revisionItemList">
                {draftItems.map((item, index) => (
                  <section key={item.item_id} className="revisionItem">
                    <header>
                      <span>#{index + 1}</span>
                      <code>{item.item_id.slice(0, 8)}</code>
                      <small>{item.source_segment_ids.length} 个证据片段</small>
                    </header>
                    <textarea
                      value={item.text}
                      onChange={(event) => updateDraftItem(index, { text: event.target.value })}
                      rows={Math.max(2, item.text.split("\n").length)}
                    />
                    <div className="revisionTimingRow">
                      <label>
                        <span>开始 ms</span>
                        <input
                          type="number"
                          min={0}
                          value={item.start_ms}
                          onChange={(event) => updateDraftItem(index, { start_ms: Number(event.target.value) })}
                        />
                      </label>
                      <label>
                        <span>结束 ms</span>
                        <input
                          type="number"
                          min={0}
                          value={item.end_ms}
                          onChange={(event) => updateDraftItem(index, { end_ms: Number(event.target.value) })}
                        />
                      </label>
                      <button type="button" className="secondary" onClick={() => mergeWithPrevious(index)} disabled={index === 0 || busy}>与上一条合并</button>
                      <button type="button" className="secondary" onClick={() => splitItem(index)} disabled={busy}>拆分</button>
                    </div>
                    <details>
                      <summary>来源 Segment ID</summary>
                      <code>{item.source_segment_ids.join(", ")}</code>
                    </details>
                  </section>
                ))}
              </div>

              <div className="revisionSaveBar">
                <span>保存会创建 Revision v{Math.max(...revisions.map((item) => item.version), 0) + 1}，不会覆盖当前版本。</span>
                <button type="button" onClick={() => void saveRevisionVersion()} disabled={busy || draftItems.length === 0}>保存新版本</button>
              </div>
            </>
          )}
        </article>
      </section>

      {selectedRevision !== null ? (
        <section className="packageCard revisionDiffPanel">
          <div className="packageSectionHeading">
            <div><span className="label">Version diff</span><h2>版本差异</h2></div>
            {revisionDiff === null ? (
              <span className="revisionDiffSummary">初始版本，无父版本</span>
            ) : (
              <span className="revisionDiffSummary">新增 {revisionDiff.added} · 删除 {revisionDiff.removed} · 修改 {revisionDiff.changed}</span>
            )}
          </div>
          {parentRevision !== null ? (
            <div className="revisionDiffColumns">
              <div>
                <h3>父版本 v{parentRevision.version}</h3>
                <ol>{parentRevision.content.items.map((item) => <li key={item.item_id}>{item.text}</li>)}</ol>
              </div>
              <div>
                <h3>当前版本 v{selectedRevision.version}</h3>
                <ol>{selectedRevision.content.items.map((item) => <li key={item.item_id}>{item.text}</li>)}</ol>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="packageCard packageEvidenceIndex">
        <div className="packageSectionHeading">
          <div>
            <span className="label">Frozen Package evidence</span>
            <h2>有效源条目</h2>
          </div>
          <strong>{packageSourceItems.length}</strong>
        </div>
        {packageSourceItems.length === 0 ? (
          <p className="emptyState">选择含有效源文档的 Frozen Package。</p>
        ) : (
          <ol className="packageEvidenceItemList">
            {packageSourceItems.map((item, index) => (
              <li id={`package-item-${item.item_id}`} key={item.item_id}>
                <div>
                  <span>#{index + 1}</span>
                  <time>{formatTime(item.start_ms)}–{formatTime(item.end_ms)}</time>
                  <code>{item.item_id}</code>
                </div>
                <p>{item.text}</p>
                <small>Segments: {item.source_segment_ids.join(", ")}</small>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="packageTranscriptGrid">
        <article className="packageCard">
          <div className="packageSectionHeading"><div><span className="label">Stage 1 evidence</span><h2>原始 Final 字幕</h2></div><strong>{segments.length}</strong></div>
          <ol className="packageTranscriptList">
            {segments.map((item) => (
              <li key={item.id}><time>{formatTime(item.audio_start_ms)}</time><p>{item.display_text}</p></li>
            ))}
          </ol>
        </article>
        <article className="packageCard">
          <div className="packageSectionHeading"><div><span className="label">Live translation Final</span><h2>实时译文</h2></div><strong>{translations.length}</strong></div>
          {translationGroups.map(([language, items]) => (
            <section key={language} className="packageTranslationGroup">
              <h3>{language}</h3>
              <ol className="packageTranscriptList">
                {items.map((item) => (
                  <li key={item.id}><time>{formatTime(item.audio_start_ms)}</time><p>{item.text}</p></li>
                ))}
              </ol>
            </section>
          ))}
          {translationGroups.length === 0 ? <p className="emptyState">此 Session 没有实时译文 Final。</p> : null}
        </article>
      </section>
    </main>
  );
}
