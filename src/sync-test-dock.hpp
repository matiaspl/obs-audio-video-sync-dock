#pragma once
#include <QFrame>
#include <QPushButton>
#include <QLabel>
#include <QComboBox>
#include <deque>
#include <obs.hpp>
#include "sync-test-output.hpp"

class SyncTestDock : public QFrame {
	Q_OBJECT

public:
	SyncTestDock(QWidget *parent = nullptr);
	~SyncTestDock();

private:
	QPushButton *startButton = nullptr;
	QPushButton *resetButton = nullptr;
	QComboBox *modeSelector = nullptr;

	QLabel *latencyLabel = nullptr;
	QLabel *latencyDisplay = nullptr;
	QLabel *latencyPolarity = nullptr;
	QLabel *glassToGlassDisplay = nullptr;
	QLabel *indexDisplay = nullptr;
	QLabel *frequencyDisplay = nullptr;
	QLabel *videoIndexDisplay = nullptr;
	QLabel *audioIndexDisplay = nullptr;

private:
	OBSOutput sync_test;

private:
	int last_video_ix;
	int last_audio_ix;
	int missed_video_ix;
	int missed_audio_ix;
	int received_video_ix;
	int received_audio_ix;
	int received_video_index_max = 0;
	int received_audio_index_max = 0;
	int audio_index_max = 0;
	int active_detect_mode = SYNC_TEST_DETECT_AV_OFFSET;
	std::deque<int64_t> av_offset_samples;

private:
	void on_start_stop();
	void on_reset();
	void reset_analysis_stats(int detect_mode);

	void on_video_marker_found(video_marker_found_s data);
	void on_audio_marker_found(audio_marker_found_s data);
	void on_sync_found(sync_index data);

	static void cb_video_marker_found(void *param, calldata_t *cd);
	static void cb_audio_marker_found(void *param, calldata_t *cd);
	static void cb_sync_found(void *param, calldata_t *cd);
};
