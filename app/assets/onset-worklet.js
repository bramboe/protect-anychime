/* Process locally; report only level and audio-clock time, never sample arrays. */
class CalibrationMeter extends AudioWorkletProcessor{
 process(inputs,outputs){const input=inputs[0]?.[0];for(const output of outputs)for(const channel of output)channel.fill(0);if(input?.length){let sum=0;for(const sample of input)sum+=sample*sample;this.port.postMessage({time:currentTime,rms:Math.sqrt(sum/input.length)});}return true;}
}
registerProcessor('calibration-meter',CalibrationMeter);
