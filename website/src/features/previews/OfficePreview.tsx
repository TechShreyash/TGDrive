import {
    ActionIcon,
    Group,
    NativeSelect,
    Stack,
    Table,
    Text,
    Title,
} from "@mantine/core";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { errorMessage, request } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import { fileUrl } from "../../shared/types";
import "./OfficePreview.css";

type OfficeDocument = {
    kind: "document";
    paragraphs: { text: string; heading: boolean }[];
    truncated: boolean;
};

type Worksheet = {
    kind: "spreadsheet";
    sheets: string[];
    selected: number;
    rows: string[][];
    truncated: boolean;
};

type Presentation = {
    kind: "presentation";
    total: number;
    selected: number;
    paragraphs: { text: string }[];
    images: { mime: string; data: string }[];
    truncated: boolean;
};

type OfficeContent = OfficeDocument | Worksheet | Presentation;

export default function OfficePreview({
    path,
    auth,
}: {
    path: string;
    auth: string;
}) {
    const [entry, setEntry] = useState(0);
    const [attempt, setAttempt] = useState(0);
    const [data, setData] = useState<OfficeContent | null>(null);
    const [error, setError] = useState("");
    useEffect(() => {
        const controller = new AbortController();
        const params = new URLSearchParams({ path });
        if (auth) params.set("auth", auth);
        if (entry !== 0) params.set("entry", String(entry));
        void request<OfficeContent>("/api/v1/books?" + params, undefined, {
            signal: controller.signal,
        })
            .then((result) => {
                if (!controller.signal.aborted) setData(result);
            })
            .catch((cause) => {
                if (!controller.signal.aborted) setError(errorMessage(cause));
            });
        return () => controller.abort();
    }, [path, auth, entry, attempt]);

    const ready = data && (data.kind === "document" || data.selected === entry);
    const requestedEntry =
        data?.kind === "presentation"
            ? "slide " + (entry + 1)
            : data?.kind === "spreadsheet"
              ? "worksheet “" + data.sheets[entry] + "”"
              : "Office preview";
    const changeEntry = (index: number) => {
        setError("");
        setEntry(index);
    };
    return (
        <Stack className="office-preview" p="md" w="100%" maw={1000} mx="auto">
            {data?.kind === "spreadsheet" && (
                <div className="office-sheet-picker">
                    <NativeSelect
                        label="Worksheet"
                        value={String(entry)}
                        onChange={(event) =>
                            changeEntry(Number(event.currentTarget.value))
                        }
                        data={data.sheets.map((label, index) => ({
                            label,
                            value: String(index),
                        }))}
                        maw={320}
                    />
                </div>
            )}
            {data?.kind === "presentation" && (
                <Group
                    className="office-slide-controls"
                    justify="center"
                    gap="md"
                >
                    <ActionIcon
                        variant="default"
                        size={44}
                        aria-label="Previous slide"
                        disabled={entry === 0}
                        onClick={() => changeEntry(entry - 1)}
                    >
                        <ChevronLeft size={18} />
                    </ActionIcon>
                    <Text size="sm" fw={600} role="status">
                        Slide {data.selected + 1} of {data.total}
                    </Text>
                    <ActionIcon
                        variant="default"
                        size={44}
                        aria-label="Next slide"
                        disabled={entry + 1 >= data.total}
                        onClick={() => changeEntry(entry + 1)}
                    >
                        <ChevronRight size={18} />
                    </ActionIcon>
                </Group>
            )}
            {error ? (
                <>
                    <ErrorNotice
                        message={
                            "Could not read " + requestedEntry + ". " + error
                        }
                    />
                    <button
                        className="secondary-button"
                        type="button"
                        onClick={() => {
                            setError("");
                            setAttempt((value) => value + 1);
                        }}
                    >
                        Try again
                    </button>
                    <a className="inline-link" href={fileUrl(path, auth, true)}>
                        Download original
                    </a>
                </>
            ) : !ready ? (
                <div role="status">
                    <Spinner /> Reading {requestedEntry}…
                </div>
            ) : (
                <>
                    {data.truncated && (
                        <Text c="dimmed" size="sm">
                            Showing part of this file.{" "}
                            <a
                                className="inline-link"
                                href={fileUrl(path, auth, true)}
                            >
                                Download original
                            </a>
                        </Text>
                    )}
                    {data.kind === "presentation" ? (
                        <Stack
                            component="article"
                            aria-label={
                                "Slide " + (data.selected + 1) + " content"
                            }
                            className="office-slide"
                            gap="sm"
                        >
                            {data.paragraphs.map(({ text }, index) => (
                                <Text
                                    key={index}
                                    component="p"
                                    fw={index === 0 ? 650 : undefined}
                                >
                                    {text}
                                </Text>
                            ))}
                            {data.images.map(
                                ({ mime, data: contents }, index) => (
                                    <img
                                        key={index}
                                        src={
                                            "data:" +
                                            mime +
                                            ";base64," +
                                            contents
                                        }
                                        alt={
                                            "Image " +
                                            (index + 1) +
                                            " on slide " +
                                            (data.selected + 1)
                                        }
                                    />
                                ),
                            )}
                            {!data.truncated &&
                                !data.paragraphs.length &&
                                !data.images.length && (
                                    <Text c="dimmed">
                                        This slide has no previewable text or
                                        images.
                                    </Text>
                                )}
                        </Stack>
                    ) : data.kind === "document" ? (
                        data.paragraphs.length ? (
                            <Stack
                                component="article"
                                className="office-document"
                                gap="sm"
                            >
                                {data.paragraphs.map(
                                    ({ heading, text }, index) =>
                                        heading ? (
                                            <Title key={index} order={3}>
                                                {text}
                                            </Title>
                                        ) : (
                                            <Text key={index} component="p">
                                                {text}
                                            </Text>
                                        ),
                                )}
                            </Stack>
                        ) : (
                            <Text>This document has no text to preview.</Text>
                        )
                    ) : data.rows.length ? (
                        <Table.ScrollContainer
                            className="office-sheet-scroll"
                            minWidth={44 + data.rows[0].length * 144}
                            type="native"
                            tabIndex={0}
                            role="region"
                            aria-label={
                                data.sheets[data.selected] + " worksheet cells"
                            }
                        >
                            <Table
                                className="office-sheet"
                                layout="fixed"
                                captionSide="top"
                                striped
                                withColumnBorders
                            >
                                <colgroup>
                                    <col className="office-sheet-row-number" />
                                </colgroup>
                                <Table.Caption>
                                    {data.sheets[data.selected]}
                                </Table.Caption>
                                <Table.Thead>
                                    <Table.Tr>
                                        <Table.Th scope="col">Row</Table.Th>
                                        {data.rows[0].map((_, index) => (
                                            <Table.Th key={index} scope="col">
                                                {String.fromCharCode(
                                                    65 + index,
                                                )}
                                            </Table.Th>
                                        ))}
                                    </Table.Tr>
                                </Table.Thead>
                                <Table.Tbody>
                                    {data.rows.map((row, index) => (
                                        <Table.Tr key={index}>
                                            <Table.Th scope="row">
                                                {index + 1}
                                            </Table.Th>
                                            {row.map((value, column) => (
                                                <Table.Td key={column}>
                                                    {value}
                                                </Table.Td>
                                            ))}
                                        </Table.Tr>
                                    ))}
                                </Table.Tbody>
                            </Table>
                        </Table.ScrollContainer>
                    ) : (
                        <Text>This worksheet is empty.</Text>
                    )}
                </>
            )}
        </Stack>
    );
}
