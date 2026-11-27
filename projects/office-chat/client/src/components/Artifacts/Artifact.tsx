import React, { useEffect, useCallback, useRef, useState } from 'react';
import throttle from 'lodash/throttle';
import { visit } from 'unist-util-visit';
import { useSetRecoilState } from 'recoil';
import { useLocation } from 'react-router-dom';
import type { Pluggable } from 'unified';
import type { Artifact } from '~/common';
import { A4_DESIGN_ARTIFACT_TYPE } from '~/common/artifacts';
import { useMessageContext, useArtifactContext } from '~/Providers';
import { logger, extractContent, isArtifactRoute } from '~/utils';
import { normalizeArtifactType } from '~/utils/artifacts';
import { parseOfficeDesign, readDesignChanges } from '~/utils/officeDesign';
import { artifactsState, artifactsVisibility, currentArtifactId } from '~/store/artifacts';
import ArtifactButton from './ArtifactButton';

export const artifactPlugin: Pluggable = () => {
  return (tree, file) => {
    visit(tree, ['textDirective', 'leafDirective', 'containerDirective'], (node, index, parent) => {
      if (node.type === 'textDirective') {
        const replacementText = `:${node.name}`;
        if (parent && Array.isArray(parent.children) && typeof index === 'number') {
          parent.children[index] = {
            type: 'text',
            value: replacementText,
          };
        }
      }
      if (node.name !== 'artifact') {
        return;
      }
      if (node.type === 'containerDirective' && typeof node.attributes?.type === 'string'
          && normalizeArtifactType(node.attributes.type) === 'text/markdown') {
        const first = node.children[0];
        const last = node.children[node.children.length - 1];
        // A single outer fence is the model's documented transport for an artifact.
        // Otherwise preserve the original Markdown before React flattens its children.
        const wrapped = first === last && first?.type === 'code' && (!first.lang || /^(?:md|markdown)$/i.test(first.lang));
        const start = first?.position?.start.offset;
        const end = last?.position?.end.offset;
        if (!wrapped && typeof start === 'number' && typeof end === 'number' && typeof file.value === 'string') {
          node.children = [{type:'text', value:file.value.slice(start, end)}];
        }
      }
      node.data = {
        hName: node.name,
        hProperties: node.attributes,
        ...node.data,
      };
      return node;
    });
  };
};

const defaultTitle = 'untitled';
const defaultType = 'unknown';
const defaultIdentifier = 'lc-no-identifier';

export function Artifact({
  node: _node,
  ...props
}: Artifact & {
  children: React.ReactNode | { props: { children: React.ReactNode } };
  node: unknown;
}) {
  const location = useLocation();
  const { messageId, conversationId, isSubmitting } = useMessageContext();
  const { getNextIndex, resetCounter } = useArtifactContext();
  const artifactIndex = useRef(getNextIndex(false)).current;

  const setArtifacts = useSetRecoilState(artifactsState);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const openedDesign = useRef<string>();
  const setVisible = useSetRecoilState(artifactsVisibility);
  const showArtifact = useSetRecoilState(currentArtifactId);

  const throttledUpdateRef = useRef(
    throttle((updateFn: () => void) => {
      updateFn();
    }, 25),
  );

  const updateArtifact = useCallback(() => {
    const content = extractContent(props.children);
    logger.log('artifacts', 'updateArtifact: content.length', content.length);

    const title = props.title ?? defaultTitle;
    const type = props.type ?? defaultType;
    const identifier = props.identifier ?? defaultIdentifier;
    const artifactKey = `${identifier}_${type}_${title}_${messageId}`
      .replace(/\s+/g, '_')
      .toLowerCase();

    throttledUpdateRef.current(() => {
      const now = Date.now();
      if (artifactKey === `${defaultIdentifier}_${defaultType}_${defaultTitle}_${messageId}`) {
        return;
      }

      const currentArtifact: Artifact = {
        id: artifactKey,
        identifier,
        title,
        type: normalizeArtifactType(type),
        content,
        messageId,
        conversationId: conversationId ?? undefined,
        index: artifactIndex,
        lastUpdateTime: now,
      };

      if (!isArtifactRoute(location.pathname)) {
        return setArtifact(currentArtifact);
      }

      setArtifacts((prevArtifacts) => {
        if (
          prevArtifacts?.[artifactKey] != null &&
          prevArtifacts[artifactKey]?.content === content
        ) {
          return prevArtifacts;
        }

        return {
          ...prevArtifacts,
          [artifactKey]: currentArtifact,
        };
      });

      setArtifact(currentArtifact);
    });
  }, [
    props.type,
    props.title,
    setArtifacts,
    props.children,
    props.identifier,
    messageId,
    conversationId,
    artifactIndex,
    location.pathname,
  ]);

  useEffect(() => {
    resetCounter();
    updateArtifact();
  }, [updateArtifact, resetCounter]);

  useEffect(() => {
    if (
      artifact?.type !== A4_DESIGN_ARTIFACT_TYPE ||
      !isSubmitting ||
      openedDesign.current === artifact.id ||
      !isArtifactRoute(location.pathname) ||
      (!parseOfficeDesign(artifact.content ?? '') && !readDesignChanges(artifact.content ?? ''))
    ) {
      return;
    }
    openedDesign.current = artifact.id;
    showArtifact(artifact.id);
    setVisible(true);
  }, [artifact, isSubmitting, location.pathname, setVisible, showArtifact]);

  return <ArtifactButton artifact={artifact} />;
}
